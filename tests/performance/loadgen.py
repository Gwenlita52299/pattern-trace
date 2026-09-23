"""开放模型压测生成器 — 固定到达率 + coordinated-omission 校正（stress-test-spec §3.6）。

为什么不直接用 Locust 打阶梯：Locust 的固定 VU 是**闭环**——服务变慢则客户端也变慢，
报价自动下降，于是系统性找不到饱和点；`constant_throughput` 只是 per-user pacing，
所有用户被阻塞时报价同样会掉。本模块按「计划发送时刻」独立于响应产生请求，
服务端卡顿不会减少已计划的负载。

两种延迟并列记录（§3.6）：
    latency_ms    实际发出 → 响应（工具口径，被 coordinated omission 低估）
    corrected_ms  计划发出 → 响应（含排队等待，判定用这一口径）

生成器自身开销也记录（CPU 秒数/系统负载/峰值在途）：**生成器成为瓶颈的档位数据无效**。

用法：
    uv run python tests/performance/loadgen.py --rate 100 --duration 60s --warmup 10s \
        --pool output/stress/pools/dev-pool.json --out /tmp/step-100.json

环境：
    STRESS_EMAIL / STRESS_PASSWORD  压测账号（默认与 seed_volume.py 一致）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import resource
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import httpx  # noqa: E402

from pool import ADDRESSES, COLD, HOT, load_pool  # noqa: E402

STRESS_EMAIL = os.environ.get("STRESS_EMAIL", "stress@perf.local")
STRESS_PASSWORD = os.environ.get("STRESS_PASSWORD", "StressPerf!2026")


# ---------------------------------------------------------------- 统计口径

def percentiles(values: list[float],
                qs: tuple[float, ...] = (50, 90, 95, 99, 99.9)) -> dict[str, float]:
    """线性插值分位数；样本不足时照样给出（调用方负责判断可信度）。"""
    if not values:
        return {f"p{q:g}": 0.0 for q in qs}
    ordered = sorted(values)
    out: dict[str, float] = {}
    for q in qs:
        pos = (len(ordered) - 1) * q / 100
        lo = int(pos)
        hi = min(lo + 1, len(ordered) - 1)
        frac = pos - lo
        out[f"p{q:g}"] = round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac, 1)
    return out


def classify(status: int | None, missing: tuple[str, ...], exc: str | None) -> str:
    """按 spec §3.2 判定：5xx/超时/非预期 4xx/缺字段都算失败。"""
    if exc:
        return "timeout" if "timeout" in exc.lower() else "transport"
    if status is None:
        return "transport"
    if status >= 500:
        return "5xx"
    if status >= 400:
        return "unexpected_4xx"
    if missing:
        return "assert_missing_field"
    return "ok"


# ---------------------------------------------------------------- 目标与调度

class Target:
    """一个被压的端点：权重 + 构造请求 + 关键字段断言。"""

    def __init__(self, name: str, weight: int, build, required: tuple[str, ...]):
        self.name = name
        self.weight = weight
        self.build = build            # (pool) -> (method, url, json|None)
        self.required = required


def build_targets(pool: dict, tier: str, analyze_weight: int,
                  seed_address: str | None) -> list[Target]:
    judgments = pool.get(tier) or pool.get(HOT) or []
    addrs = pool.get(ADDRESSES) or []
    targets = [
        Target("GET /api/v1/judgments/[id]", 60,
               lambda p, ids=judgments: ("GET",
                                         f"/api/v1/judgments/{random.choice(ids)}", None),
               ("status",)),
        Target("GET /api/v1/addresses/[addr]/subgraph", 25,
               lambda p, a=addrs: ("GET", f"/api/v1/addresses/{random.choice(a)}/subgraph",
                                   None),
               ("nodes",)),
        Target("GET /api/v1/cases", 10,
               lambda p: ("GET", f"/api/v1/cases?page={random.randint(1, 5)}", None),
               ("items",)),
        Target("GET /api/v1/patterns", 5,
               lambda p: ("GET", "/api/v1/patterns", None), ("items",)),
    ]
    if analyze_weight and seed_address:
        targets.append(Target("POST /api/v1/addresses/analyze", analyze_weight,
                              lambda p, s=seed_address: ("POST",
                                                         "/api/v1/addresses/analyze",
                                                         {"address": s}),
                              ("judgment_id",)))
    return [t for t in targets if t.weight > 0]


def pick_target(targets: list[Target], rng: random.Random) -> Target:
    return rng.choices(targets, weights=[t.weight for t in targets], k=1)[0]


def schedule(rate: float, duration: float, warmup: float, poisson: bool,
             seed: int) -> list[float]:
    """计划发送时刻（秒，相对起点）；间隔均匀或用固定种子的泊松抖动。"""
    total = duration + warmup
    count = int(rate * total)
    rng = random.Random(seed)
    times: list[float] = []
    cursor = 0.0
    for _ in range(count):
        gap = rng.expovariate(rate) if poisson else 1.0 / rate
        cursor += gap
        times.append(cursor)
    return times


# ---------------------------------------------------------------- 主循环

async def login(client: httpx.AsyncClient) -> str | None:
    r = await client.post("/api/v1/auth/login",
                          json={"email": STRESS_EMAIL, "password": STRESS_PASSWORD},
                          headers={"X-Requested-With": "XMLHttpRequest"})
    return r.json().get("access_token") if r.status_code == 200 else None


async def run(cfg) -> dict:
    payload = load_pool(cfg.pool)
    targets = build_targets(payload, cfg.tier, cfg.analyze_weight, cfg.seed_address)
    if not targets:
        raise SystemExit("[FAIL] 池内无可用 id/地址，先跑 run_stress.py pool")

    limits = httpx.Limits(max_connections=cfg.max_concurrency + 50,
                          max_keepalive_connections=cfg.max_concurrency)
    records: list[dict] = []
    peak_inflight = 0
    inflight = 0
    queued = 0           # 到达时刻已过才拿到并发额度：生成器侧排队（饱和信号）
    rng = random.Random(cfg.seed)
    cpu0 = resource.getrusage(resource.RUSAGE_SELF)
    wall0 = time.perf_counter()

    async with httpx.AsyncClient(base_url=cfg.host, timeout=cfg.timeout,
                                 limits=limits,
                                 headers={"X-Requested-With": "XMLHttpRequest"}) as client:
        token = await login(client)
        if token:
            client.headers["Authorization"] = f"Bearer {token}"
        loop = asyncio.get_running_loop()
        start = loop.time()
        sem = asyncio.Semaphore(cfg.max_concurrency)
        tasks: list[asyncio.Task] = []

        async def send(target: Target, intended_offset: float, index: int) -> None:
            nonlocal inflight, peak_inflight, queued
            intended = start + intended_offset
            now = loop.time()
            if now < intended:
                await asyncio.sleep(intended - now)
            await sem.acquire()
            if loop.time() > intended + 0.005:
                # 到达时刻被推迟 >5ms：并发额度被占满（生成器或被测系统已饱和）。
                # 阈值不能取 1ms——那只会数出事件循环的调度抖动，不是排队。
                queued += 1
            inflight += 1
            peak_inflight = max(peak_inflight, inflight)
            sent = loop.time()
            method, url, body = target.build(payload)
            status, missing, exc = None, (), None
            try:
                resp = await client.request(method, url, json=body)
                status = resp.status_code
                if status == 200:
                    try:
                        data = resp.json()
                        missing = tuple(f for f in target.required
                                        if not (isinstance(data, dict) and data.get(f)))
                    except Exception:
                        missing = ("<non-json>",)
            except Exception as error:
                exc = type(error).__name__ + ": " + str(error)[:80]
            done = loop.time()
            inflight -= 1
            sem.release()
            records.append({
                "endpoint": target.name,
                "status": status,
                "error_class": classify(status, missing, exc),
                "latency_ms": round((done - sent) * 1000, 1),
                "corrected_ms": round((done - intended) * 1000, 1),
                "warmup": intended_offset < cfg.warmup,
            })

        for index, offset in enumerate(schedule(cfg.rate, cfg.duration, cfg.warmup,
                                                cfg.poisson, cfg.seed)):
            now = loop.time() - start
            if offset > now:
                await asyncio.sleep(offset - now)
            tasks.append(asyncio.create_task(send(pick_target(targets, rng), offset, index)))
        await asyncio.gather(*tasks)

    wall = time.perf_counter() - wall0
    cpu1 = resource.getrusage(resource.RUSAGE_SELF)
    cpu_seconds = (cpu1.ru_utime - cpu0.ru_utime) + (cpu1.ru_stime - cpu0.ru_stime)

    measured = [r for r in records if not r["warmup"]]
    result = {
        "config": {
            "host": cfg.host, "rate": cfg.rate, "duration": cfg.duration,
            "warmup": cfg.warmup, "tier": cfg.tier, "poisson": cfg.poisson,
            "max_concurrency": cfg.max_concurrency, "seed": cfg.seed,
            "pool": str(cfg.pool), "analyze_weight": cfg.analyze_weight,
        },
        "generator": {
            "wall_seconds": round(wall, 1),
            "cpu_seconds": round(cpu_seconds, 2),
            "cpu_utilization_pct": round(cpu_seconds / wall * 100, 1),
            "loadavg_after": [round(x, 2) for x in os.getloadavg()],
            "peak_inflight": peak_inflight,
            "queued_arrivals": queued,
            "co_located_with_sut": cfg.co_located,
        },
        "aggregate": _aggregate(measured, cfg),
        "by_endpoint": {name: _aggregate([r for r in measured if r["endpoint"] == name],
                                         cfg)
                        for name in sorted({r["endpoint"] for r in measured})},
        "samples_total": len(records),
        "samples_measured": len(measured),
    }
    return result


def _aggregate(records: list[dict], cfg) -> dict:
    total = len(records)
    failures = [r for r in records if r["error_class"] != "ok"]
    classes: dict[str, int] = {}
    for r in failures:
        classes[r["error_class"]] = classes.get(r["error_class"], 0) + 1
    ok_records = [r for r in records if r["error_class"] == "ok"]
    return {
        "requests": total,
        "failures": len(failures),
        "error_rate": round(len(failures) / total, 4) if total else 0.0,
        "error_classes": classes,
        "achieved_rps": round(total / cfg.duration, 1) if cfg.duration else 0.0,
        "target_rps": cfg.rate,
        "latency_ms": percentiles([r["latency_ms"] for r in ok_records]),
        "corrected_ms": percentiles([r["corrected_ms"] for r in ok_records]),
    }


# ---------------------------------------------------------------- CLI

def _duration(value: str) -> float:
    return float(value.rstrip("s"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="开放模型压测生成器")
    parser.add_argument("--rate", type=float, required=True, help="目标到达率 RPS")
    parser.add_argument("--duration", type=_duration, default=60.0, help="测量时长（秒）")
    parser.add_argument("--warmup", type=_duration, default=10.0,
                        help="预热时长（秒），该段请求不计入统计")
    parser.add_argument("--host", default=os.environ.get("STRESS_HOST",
                                                         "http://localhost:8000"))
    parser.add_argument("--pool", required=True)
    parser.add_argument("--tier", default=HOT, choices=[HOT, COLD])
    parser.add_argument("--out", default=None, help="结果 JSON 输出路径")
    parser.add_argument("--seed", type=int, default=20260923)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-concurrency", type=int, default=256,
                        help="在途请求上限；到达时无额度即记 arrival_backlog")
    parser.add_argument("--analyze-weight", type=int, default=0,
                        help="写路径权重（默认 0：阶梯只压读路径）")
    parser.add_argument("--seed-address", default=None, help="analyze 用的种子地址")
    parser.add_argument("--poisson", action="store_true", help="泊松到达（默认均匀间隔）")
    parser.add_argument("--co-located", action="store_true",
                        help="生成器与被测系统同机（归档记录为已知偏差）")
    cfg = parser.parse_args(argv)

    result = asyncio.run(run(cfg))
    agg = result["aggregate"]
    print(f"[loadgen] rate={cfg.rate} achieved={agg['achieved_rps']} "
          f"err={agg['error_rate']:.2%} "
          f"p95={agg['corrected_ms']['p95']}ms(corrected)/"
          f"{agg['latency_ms']['p95']}ms(tool) "
          f"p99={agg['corrected_ms']['p99']}ms "
          f"gen_cpu={result['generator']['cpu_utilization_pct']}%")
    if cfg.out:
        out = Path(cfg.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(f"[loadgen] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
