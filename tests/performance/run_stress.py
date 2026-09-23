"""压测单一入口 — 环境守卫 / 抽样池 / 基线 / 清理（stress-test-spec §5）。

为什么要有这个入口：spec 的结论强绑定「环境是否 fixture+mock」「数据规模」
「id 池是否分层」「观测项是否同档采集」。散着手敲 locust 命令时这四件事都容易漏，
于是归档出来的 CSV 不可比、不可信。把守卫、抽样、采集、归档收在一处，
让「归档物自描述」成为默认行为而不是纪律要求。

用法：
    uv run python tests/performance/run_stress.py guard
    uv run python tests/performance/run_stress.py pool --tag dev
    uv run python tests/performance/run_stress.py baseline --tag dev-read --profile read --vu 10
    uv run python tests/performance/run_stress.py cleanup

环境：
    DATABASE_URL   默认 postgresql://pt:pt@localhost:5432/patterntrace
    STRESS_HOST    被测服务地址，默认 http://localhost:8000

P1 待实现：`ladder`（1→10→50→100→200 自动阶梯）与 `compare`（与基线对比劣化）。
本文件不实现这两项时会显式拒绝（退出码 2），不做静默空跑。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
# 与 seed_volume.py / perf_phase6.py 同一测试密钥：Settings 必填项，
# 本脚本只读库与观测，不校验 JWT，但缺省会 fail-fast
os.environ.setdefault("JWT_SECRET",
                      "0123456789abcdef0123456789abcdef"
                      "0123456789abcdef0123456789abcdef")

DEFAULT_DB = "postgresql://pt:pt@localhost:5432/patterntrace"
DEFAULT_HOST = os.environ.get("STRESS_HOST", "http://localhost:8000")
OUTPUT_ROOT = ROOT / "output" / "stress"

REQUIRED_MODE = "fixture"
REQUIRED_PROVIDER = "mock"
QUEUES = ("q_analysis", "q_report", "q_index")


def _load_pool_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("stress_perf_pool", HERE / "pool.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pool = _load_pool_module()


def _db_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DB)


def _engine():
    """复用后端的 engine 构造（驱动/SSL 参数与容器内一致，避免手写 DSN 漂移）。"""
    from backend.api.app import get_db_engine

    return get_db_engine()


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- guard

def _compose_backend_env() -> dict[str, str] | None:
    """读 backend 容器的环境变量；compose 不可用（未启动/无 docker）返回 None。"""
    try:
        proc = subprocess.run(
            ["docker", "compose", "exec", "-T", "backend", "env"],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    env: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            env[key] = value
    return env


def cmd_guard(args) -> int:
    env = _compose_backend_env()
    if env is None:
        if args.allow_non_fixture:
            print("[warn] 无法读取 compose 环境（远程档？）——已显式 --allow-non-fixture，"
                  "归档必须记录该事实")
            return 0
        print("[FAIL] 无法确证被测服务运行在 fixture+mock 下。"
              "请用 GRAPH_DATA_MODE=fixture LLM_PROVIDER=mock docker compose up -d 启动，"
              "或显式传 --allow-non-fixture（仅远程档）。")
        return 1
    mode = env.get("GRAPH_DATA_MODE", "")
    provider = env.get("LLM_PROVIDER", "")
    ok = mode == REQUIRED_MODE and provider == REQUIRED_PROVIDER
    print(f"[{'ok' if ok else 'FAIL'}] 环境守卫：GRAPH_DATA_MODE={mode} LLM_PROVIDER={provider}")
    if ok:
        return 0
    if args.allow_non_fixture:
        print("[warn] 已显式 --allow-non-fixture，继续；归档将记录该事实")
        return 0
    print(f"[FAIL] 压测要求 {REQUIRED_MODE}+{REQUIRED_PROVIDER}："
          "真实数据源会引入网络 RTT，真实 LLM 会引入 rate limit 与费用，结论不可用")
    return 1


# ---------------------------------------------------------------- pool

def _scalar(conn, sql: str, **params):
    from sqlalchemy import text

    return conn.execute(text(sql), params).scalar_one()


def _rows(conn, sql: str, **params) -> list[str]:
    from sqlalchemy import text

    return [r[0] for r in conn.execute(text(sql), params).all()]


def cmd_pool(args) -> int:
    engine = _engine()
    hot_window = f"{args.hot_hours} hours"
    with engine.connect() as conn:
        hot = _rows(conn, """
            SELECT id FROM judgments
            WHERE status='completed' AND created_at >= now() - CAST(:w AS interval)
            ORDER BY random() LIMIT :n""", w=hot_window, n=args.hot)
        cold = _rows(conn, """
            SELECT id FROM judgments
            WHERE status='completed' AND created_at < now() - CAST(:w AS interval)
            ORDER BY random() LIMIT :n""", w=hot_window, n=args.cold)
        addrs = _rows(conn, """
            SELECT address FROM (
                SELECT DISTINCT address FROM judgments
                WHERE status='completed' AND address LIKE 'STRESS%') s
            ORDER BY random() LIMIT :n""", n=args.addresses)
        scale = {
            "judgments_total": _scalar(conn, "SELECT count(*) FROM judgments"),
            "judgments_completed": _scalar(
                conn, "SELECT count(*) FROM judgments WHERE status='completed'"),
            "cases_total": _scalar(conn, "SELECT count(*) FROM cases"),
            "stress_judgments": _scalar(
                conn, "SELECT count(*) FROM judgments WHERE address LIKE 'STRESS%'"),
        }
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "hot_window_hours": args.hot_hours,
        "scale": scale,
        "sizes": {"hot": len(hot), "cold": len(cold), "addresses": len(addrs)},
    }
    path = OUTPUT_ROOT / "pools" / f"{args.tag}-pool.json"
    pool.write_pool(path, hot=hot, cold=cold, addresses=addrs, meta=meta)
    print(f"[ok] 池已写入 {_rel(path)} "
          f"hot={len(hot)} cold={len(cold)} addresses={len(addrs)}")
    print(f"     data scale: {scale}")
    payload = pool.load_pool(path)
    if not pool.pool_usable_as_baseline(payload):
        print("[FAIL] 热/冷两档必须都非空才可作为基线（spec §2.3）——"
              "冷档为空通常说明数据量不足或全部落在热窗口内")
        return 1
    return 0


# ---------------------------------------------------------------- observations

def _run(cmd: list[str], timeout=60) -> str:
    try:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                              timeout=timeout)
        return proc.stdout.strip() or proc.stderr.strip()
    except Exception as exc:  # 观测项失败不能中断压测
        return f"<unavailable: {exc}>"


def _compose(*args: str) -> list[str]:
    return ["docker", "compose", *args]


def collect_observations(engine=None) -> dict:
    """每档稳态期的服务端观测项（spec §3.4）。任何一项失败都不影响其余项。"""
    obs: dict = {"captured_at": datetime.now(timezone.utc).isoformat()}
    obs["docker_stats"] = _run(_compose("stats", "--no-stream",
                                        "--format",
                                        "{{.Name}},{{.CPUPerc}},{{.MemUsage}}"),
                               timeout=60)
    obs["redis_clients"] = _run(_compose("exec", "-T", "redis",
                                         "redis-cli", "info", "clients"))
    obs["redis_stats"] = _run(_compose("exec", "-T", "redis",
                                       "redis-cli", "info", "stats"))
    obs["queues"] = {
        name: _run(_compose("exec", "-T", "redis", "redis-cli", "llen", name))
        for name in QUEUES
    }
    try:
        from sqlalchemy import text

        eng = engine or _engine()
        with eng.connect() as conn:
            obs["pg_activity"] = conn.execute(
                text("SELECT count(*) FROM pg_stat_activity")).scalar_one()
            obs["pg_wait_events"] = [
                {"wait_event": r[0], "n": r[1]} for r in conn.execute(
                    text("SELECT wait_event, count(*) FROM pg_stat_activity "
                         "GROUP BY 1 ORDER BY 2 DESC")).all()]
            obs["analysis_spans"] = {
                "rows": conn.execute(text(
                    "SELECT count(*) FROM analysis_spans")).scalar_one(),
                "traces": conn.execute(text(
                    "SELECT count(DISTINCT trace_id) FROM analysis_spans")).scalar_one(),
            }
    except Exception as exc:
        obs["pg_error"] = str(exc)
    return obs


def _parse_locust_stats(csv_path: Path) -> dict:
    """从 locust `*_stats.csv` 提取聚合与轮询行的 p95（spec §3.1/§3.3）。"""
    import csv

    if not csv_path.exists():
        return {"error": f"missing {csv_path.name}"}
    rows = list(csv.DictReader(csv_path.open()))
    out: dict = {"total_requests": 0, "total_failures": 0, "by_name": {}}
    for row in rows:
        name = row.get("Name") or ""
        try:
            requests = int(float(row.get("Request Count") or 0))
            failures = int(float(row.get("Failure Count") or 0))
        except ValueError:
            continue
        p95 = row.get("95%") or ""
        try:
            p95_value = round(float(p95), 1)
        except ValueError:
            p95_value = None
        if name == "Aggregated":
            out["p95_ms"] = p95_value
            out["total_requests"] = requests
            out["total_failures"] = failures
            out["error_rate"] = round(failures / requests, 4) if requests else None
        else:
            out["by_name"][name] = {"requests": requests, "failures": failures,
                                    "p95_ms": p95_value}
    return out


# ---------------------------------------------------------------- baseline

def _rel(path) -> str:
    """归档里一律写仓库相对路径；仓库外路径原样保留（不抛错）。"""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def cmd_baseline(args) -> int:
    if cmd_guard(argparse.Namespace(allow_non_fixture=args.allow_non_fixture)) != 0:
        return 1
    if args.pool:
        pool_path = Path(args.pool)
        if not pool_path.is_absolute():
            pool_path = ROOT / pool_path
    else:
        pool_path = OUTPUT_ROOT / "pools" / f"{args.tag}-pool.json"
    if not pool_path.exists():
        print(f"[info] 池文件不存在，先按 {args.tag} 生成")
        rc = cmd_pool(argparse.Namespace(tag=args.tag, hot=args.hot, cold=args.cold,
                                         addresses=args.addresses,
                                         hot_hours=args.hot_hours))
        if rc != 0:
            return rc
    payload = pool.load_pool(pool_path)
    usable = pool.pool_usable_as_baseline(payload)
    if not usable and not args.allow_derived_pool:
        print("[FAIL] 池不满足基线口径（热/冷两档需都非空）；"
              "只做冒烟请显式传 --allow-derived-pool，归档会标注降级")
        return 1

    started = datetime.now(timezone.utc)
    archive = OUTPUT_ROOT / f"{started:%Y%m%d}-{args.tag}"
    archive.mkdir(parents=True, exist_ok=True)

    rate = max(args.vu // 10, 1)
    prefix = archive / "locust"
    env = dict(os.environ)
    env.update({
        "STRESS_PROFILE": args.profile,
        "STRESS_ID_POOL_FILE": str(pool_path),
        "STRESS_POOL_TIER": args.tier,
    })
    if args.mock_scenario:
        env["STRESS_MOCK_SCENARIO"] = args.mock_scenario
    if args.expect_failure:
        env["STRESS_EXPECT_FAILURE"] = "1"

    cmd = [sys.executable, "-m", "locust", "-f", str(HERE / "locustfile.py"),
           "--headless", "--csv", str(prefix), "--only-summary",
           "-u", str(args.vu), "-r", str(rate), "-t", args.duration,
           "--host", args.host]
    print(f"[run] {' '.join(cmd)}  (profile={args.profile} tier={args.tier} "
          f"pool={pool_path.name})")
    proc = subprocess.run(cmd, cwd=ROOT, env=env)
    if proc.returncode not in (0, 1):   # locust 在有失败请求时退出码 1：仍要归档
        print(f"[FAIL] locust 退出码 {proc.returncode}")
        return proc.returncode

    observations = collect_observations()
    stats = _parse_locust_stats(archive / "locust_stats.csv")
    summary = {
        "tag": args.tag,
        "profile": args.profile,
        "pool_tier": args.tier,
        "tier_note": "热/冷两档分别归档，不可合并统计" if usable else "降级运行",
        "pool_origin": _rel(pool_path),
        "pool_usable_as_baseline": usable,
        "host": args.host,
        "vu": args.vu,
        "duration": args.duration,
        "started_at": started.isoformat(),
        "git_sha": _git_sha(),
        "hardware": {
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "pool_meta": payload.get("meta", {}),
        "locust": stats,
        "guard": "passed",
        "observations_file": "observations.json",
        "mock_scenario": args.mock_scenario or None,
    }
    (archive / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    (archive / "observations.json").write_text(
        json.dumps(observations, ensure_ascii=False, indent=2) + "\n")

    ok = True
    if stats.get("error_rate") is not None and stats["error_rate"] >= 0.01:
        print(f"[FAIL] 错误率 {stats['error_rate']:.2%} ≥ 1%（spec §3.3）")
        ok = False
    poll = next((v for k, v in stats.get("by_name", {}).items()
                 if "judgments/[id]" in k), None)
    if args.tier == pool.HOT and poll and poll.get("p95_ms") is not None:
        verdict = poll["p95_ms"] <= 300
        print(f"[{'ok' if verdict else 'FAIL'}] 热档轮询 p95 = {poll['p95_ms']}ms "
              f"(阈值 300ms)")
        ok = ok and verdict
    print(f"[ok] 归档 {_rel(archive)}：locust CSV + summary.json + observations.json")
    return 0 if ok else 1


# ---------------------------------------------------------------- cleanup

def cmd_cleanup(args) -> int:
    cmd = [sys.executable, str(HERE / "seed_volume.py"), "--cleanup"]
    print(f"[run] {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=ROOT).returncode


# ---------------------------------------------------------------- ladder

POLL_ENDPOINT = "GET /api/v1/judgments/[id]"

# spec §1.0 固化的容量目标（待生产数据校准；可被 CLI 覆盖并写进归档）
GENERATOR_CPU_LIMIT = 60.0   # 生成器 CPU 超过此值，该档数据无效（spec §3.6）

CAPACITY_TARGET = {
    "target_rps": 250.0,
    "poll_p95_ms": 300.0,
    "poll_p99_ms": 1000.0,
    "error_rate": 0.01,
    "derivation": "20 并发调查员 × 5 RPS 轮询 ≈ 125 RPS 峰值，按 2× 峰值验证（spec §1.0）",
}


def merge_generator_results(parts: list[dict]) -> dict:
    """合并多个生成器进程的结果（多进程压测机）。

    为什么按「最大」而不是「平均」合并分位数：同一档位内 N 个进程各自独立，
    容量结论必须由**最慢的那个**决定（保守口径）；CPU% 同理取最大——每个进程
    是独立事件循环，任一循环饱和就说明该档位混入了压测机排队。
    """
    if not parts:
        return {}
    if len(parts) == 1:
        return parts[0]

    def pmax(field: str) -> dict:
        keys = set()
        for part in parts:
            keys |= set((part.get("aggregate", {}).get(field) or {}).keys())
        return {k: max((p["aggregate"].get(field) or {}).get(k) or 0 for p in parts)
                for k in sorted(keys)}

    endpoints: dict[str, dict] = {}
    for part in parts:
        for name, item in (part.get("by_endpoint") or {}).items():
            merged = endpoints.setdefault(name, {
                "requests": 0, "failures": 0, "error_rate": 0.0, "error_classes": {},
                "latency_ms": {}, "corrected_ms": {}})
            merged["requests"] += item.get("requests") or 0
            merged["failures"] += item.get("failures") or 0
            for field in ("latency_ms", "corrected_ms"):
                for k, v in (item.get(field) or {}).items():
                    merged[field][k] = max(merged[field].get(k) or 0, v)
            for cls, count in (item.get("error_classes") or {}).items():
                merged["error_classes"][cls] = merged["error_classes"].get(cls, 0) + count
    for item in endpoints.values():
        item["error_rate"] = (round(item["failures"] / item["requests"], 4)
                              if item["requests"] else 0.0)

    requests = sum(p["aggregate"]["requests"] for p in parts)
    failures = sum(p["aggregate"]["failures"] for p in parts)
    classes: dict[str, int] = {}
    for part in parts:
        for cls, count in (part["aggregate"].get("error_classes") or {}).items():
            classes[cls] = classes.get(cls, 0) + count
    return {
        "config": {**parts[0].get("config", {}),
                   "rate": sum(p["config"]["rate"] for p in parts),
                   "generators": len(parts)},
        "generator": {
            "wall_seconds": max(p["generator"]["wall_seconds"] for p in parts),
            "cpu_utilization_pct": max(p["generator"]["cpu_utilization_pct"]
                                       for p in parts),
            "cpu_total_pct": round(sum(p["generator"]["cpu_utilization_pct"]
                                       for p in parts), 1),
            "queued_arrivals": sum(p["generator"].get("queued_arrivals") or 0
                                   for p in parts),
            "peak_inflight": sum(p["generator"].get("peak_inflight") or 0
                                 for p in parts),
            "loadavg_after": max((p["generator"].get("loadavg_after") or [0])[0]
                                 for p in parts),
            "co_located_with_sut": any(p["generator"].get("co_located_with_sut")
                                       for p in parts),
            "processes": len(parts),
        },
        "aggregate": {
            "requests": requests, "failures": failures,
            "error_rate": round(failures / requests, 4) if requests else 0.0,
            "error_classes": classes,
            "achieved_rps": round(sum(p["aggregate"]["achieved_rps"] for p in parts), 1),
            "target_rps": round(sum(p["aggregate"]["target_rps"] for p in parts), 1),
            "latency_ms": pmax("latency_ms"), "corrected_ms": pmax("corrected_ms"),
        },
        "by_endpoint": endpoints,
        "samples_total": sum(p.get("samples_total") or 0 for p in parts),
        "samples_measured": sum(p.get("samples_measured") or 0 for p in parts),
    }


def _generator_context() -> dict:
    """P1c：记录压测机是否与被测系统同机（同机时资源观测不可作为容量依据）。"""
    cid = _run(_compose("ps", "-q", "backend"), timeout=30).strip()
    return {
        "co_located_with_sut": bool(cid),
        "cpu_count": os.cpu_count(),
        "loadavg_before": [round(x, 2) for x in os.getloadavg()],
    }


def backend_cpu_pct(observations: dict | None) -> float | None:
    """从 docker stats 文本里取 backend 容器 CPU%（拐点可信度判定用）。"""
    for line in ((observations or {}).get("docker_stats") or "").splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 2 and parts[0].endswith("backend-1"):
            try:
                return float(parts[1].rstrip("%"))
            except ValueError:
                return None
    return None


def _step_metrics(step: dict, observations: dict | None = None) -> dict:
    """从 loadgen 单档结果抽取判定所需指标（只做提取，不做判定）。"""
    agg = step.get("aggregate") or {}
    corrected = agg.get("corrected_ms") or {}
    poll = ((step.get("by_endpoint") or {}).get(POLL_ENDPOINT) or {})
    poll_corrected = poll.get("corrected_ms") or {}
    gen = step.get("generator") or {}
    return {
        "target_rps": (step.get("config") or {}).get("rate"),
        "achieved_rps": agg.get("achieved_rps"),
        "error_rate": agg.get("error_rate"),
        "p95_tool_ms": (agg.get("latency_ms") or {}).get("p95"),
        "p95_corrected_ms": corrected.get("p95"),
        "p99_corrected_ms": corrected.get("p99"),
        "poll_p95_corrected_ms": poll_corrected.get("p95"),
        "poll_p99_corrected_ms": poll_corrected.get("p99"),
        "poll_error_rate": poll.get("error_rate"),
        "generator_cpu_pct": gen.get("cpu_utilization_pct"),
        "generator_bound": (gen.get("cpu_utilization_pct") or 0) >= GENERATOR_CPU_LIMIT,
        "co_gap_x": (round(corrected.get("p95") / (agg.get("latency_ms") or {}).get("p95"), 1)
                     if (corrected.get("p95") and (agg.get("latency_ms") or {}).get("p95"))
                     else None),
        "queued_arrivals": gen.get("queued_arrivals"),
        "peak_inflight": gen.get("peak_inflight"),
        "samples_measured": step.get("samples_measured"),
        "loadavg_after": gen.get("loadavg_after"),
        "server_cpu_pct": backend_cpu_pct(observations),
        "knee_reasons": [],
        "knee": False,
    }


def detect_knee(steps: list[dict],
                target_rps: float | None = None) -> dict:
    """spec §3.5 的**唯一**拐点判定点：四个条件任一成立即该档触拐点。

    判定集中在这里而不是散在各处：否则「哪档算触拐点」会出现两套口径，
    报告与早停结论可能不一致。只做判定，不做指标提取（_step_metrics 负责）。
    """
    if not steps:
        return {"knee_at_rate": None, "reasons": [], "notes": ["无有效档位"]}
    target = target_rps or CAPACITY_TARGET["target_rps"]
    baseline = steps[0].get("p95_corrected_ms") or 0
    notes: list[str] = []
    valid: list[dict] = []
    for step in steps:
        gen_cpu = step.get("generator_cpu_pct") or 0
        step["invalid_generator_bound"] = gen_cpu >= GENERATOR_CPU_LIMIT
        if step["invalid_generator_bound"]:
            # 压测机先饱和：校正延迟里混的是生成器排队，不是服务端排队。
            # 该档既不作为拐点，也不作为容量证据（否则会把「压测机不够」误报成「服务端到顶」）。
            step["invalid_reasons"] = [
                f"生成器 CPU {gen_cpu}% ≥ {GENERATOR_CPU_LIMIT:g}%"
                + (f"，校正/工具延迟比 {step['co_gap_x']}×" if step.get("co_gap_x") else "")
                + "：该档由压测机饱和导致，不代表服务端容量，需独立压测机重跑"]
            step["knee_reasons"] = []
            step["knee"] = False
            continue
        reasons: list[str] = []
        p95 = step.get("p95_corrected_ms") or 0
        err = step.get("error_rate") or 0
        achieved = step.get("achieved_rps") or 0
        if baseline and p95 >= 3 * baseline:
            reasons.append(f"校正 p95 {p95}ms ≥ 首档 {baseline}ms 的 3×（尾延迟非线性抬升）")
        if err >= CAPACITY_TARGET["error_rate"]:
            reasons.append(f"错误率 {err:.2%} ≥ 1%")
        if achieved and achieved < 0.9 * step.get("target_rps", target):
            reasons.append(f"实测 RPS {achieved} < 目标 {step.get('target_rps', target)} 的 90%")
        step["knee_reasons"] = reasons
        step["knee"] = bool(reasons)
        # 拐点必须能被服务端资源解释：若该档服务端 CPU 仍很低，说明延迟来自压测环境
        # （同机竞争/容器网络/压测机排队），据此宣布「容量上限」是错的结论。
        cpu = step.get("server_cpu_pct")
        step["knee_trustworthy"] = not (reasons and cpu is not None and cpu < 10)
        if reasons and not step["knee_trustworthy"]:
            step["knee_reasons"] = reasons + [
                f"服务端 CPU 仅 {cpu}%：延迟抬升无法由服务端资源解释，"
                f"疑为压测环境限制（同机/容器网络），不可据此认定容量上限"]
        valid.append(step)
    knees = [s for s in valid if s.get("knee")]
    trusted = [s for s in knees if s.get("knee_trustworthy")]
    untrusted = [s for s in knees if not s.get("knee_trustworthy")]
    invalid = [s for s in steps if s.get("invalid_generator_bound")]
    if not knees:
        notes.append("有效档位内未触拐点（尾延迟未出现非线性抬升、错误率未抬头）")
    if invalid:
        notes.append("档位 " + "/".join(f"{s['target_rps']:g}" for s in invalid)
                     + " RPS 因压测机饱和数据无效，已排除在拐点判定之外")
    if untrusted:
        notes.append("档位 " + "/".join(f"{s['target_rps']:g}" for s in untrusted)
                     + " RPS 的延迟抬升伴随服务端低 CPU，判为环境限制而非容量上限")
    return {
        "knee_at_rate": trusted[0]["target_rps"] if trusted else None,
        "untrusted_knee_rates": [s["target_rps"] for s in untrusted],
        "reasons": trusted[0]["knee_reasons"] if trusted else [],
        "notes": notes,
        "baseline_p95_corrected_ms": baseline,
        "highest_step_rps": steps[-1]["target_rps"],
        "validated_max_rps": max([s["target_rps"] for s in valid] or [0]),
        "invalid_steps": [s["target_rps"] for s in invalid],
    }


def decide_status(knee: dict, target: float) -> str:
    """四态结论：meets / below / environment_limited / indeterminate。

    区分 environment_limited 与 below 是本轮最重要的口径修正：延迟抬升若伴随
    服务端低 CPU，瓶颈更可能在压测环境（同机竞争、容器网络、压测机排队），
    宣布「容量上限」会把环境问题写成产品结论。
    """
    if knee.get("knee_at_rate") is not None:
        return "below" if knee["knee_at_rate"] <= target else "meets"
    if knee.get("untrusted_knee_rates"):
        return "environment_limited"
    if (knee.get("validated_max_rps") or 0) >= target:
        return "meets"
    return "indeterminate"


def cmd_ladder(args) -> int:
    if cmd_guard(argparse.Namespace(allow_non_fixture=args.allow_non_fixture)) != 0:
        return 1
    if args.pool:
        pool_path = Path(args.pool)
        if not pool_path.is_absolute():
            pool_path = ROOT / pool_path
    else:
        pool_path = OUTPUT_ROOT / "pools" / f"{args.tag}-pool.json"
    if not pool_path.exists():
        print(f"[info] 池文件不存在，先按 {args.tag} 生成")
        if cmd_pool(argparse.Namespace(tag=args.tag, hot=args.hot, cold=args.cold,
                                       addresses=args.addresses,
                                       hot_hours=args.hot_hours)) != 0:
            return 1
    payload = pool.load_pool(pool_path)
    if not pool.pool_usable_as_baseline(payload) and not args.allow_derived_pool:
        print("[FAIL] 池不满足基线口径（热/冷两档需都非空）；冒烟请传 --allow-derived-pool")
        return 1

    started = datetime.now(timezone.utc)
    archive = OUTPUT_ROOT / f"{started:%Y%m%d}-{args.tag}"
    archive.mkdir(parents=True, exist_ok=True)
    ctx = _generator_context()
    if ctx["co_located_with_sut"]:
        print("[warn] 压测机与被测系统同机（已知偏差）：资源观测不可作为容量依据，"
              "已记入归档；生产级判定需把生成器放到独立机器")
    rates = [float(x) for x in args.rates.split(",") if x.strip()]
    steps: list[dict] = []

    for rate in rates:
        parts = max(1, args.generators)
        per_rate = rate / parts
        procs, outs = [], []
        for index in range(parts):
            out = archive / (f"step-{rate:g}.json" if parts == 1
                             else f"step-{rate:g}-g{index + 1}.json")
            cmd = [sys.executable, str(HERE / "loadgen.py"), "--rate", str(per_rate),
                   "--duration", args.step_duration, "--warmup", args.warmup,
                   "--host", args.host, "--pool", str(pool_path), "--tier", args.tier,
                   "--out", str(out), "--max-concurrency", str(args.max_concurrency)]
            if args.poisson:
                cmd.append("--poisson")
            if ctx["co_located_with_sut"]:
                cmd.append("--co-located")
            procs.append(subprocess.Popen(cmd, cwd=ROOT))
            outs.append(out)
        codes = [proc.wait() for proc in procs]
        if any(code != 0 for code in codes) or not all(o.exists() for o in outs):
            print(f"[FAIL] 档位 {rate:g} 生成器失败（退出码 {codes}）")
            return 1
        merged = merge_generator_results([json.loads(o.read_text()) for o in outs])
        if parts > 1:
            (archive / f"step-{rate:g}.json").write_text(
                json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
        step_obs = collect_observations()
        (archive / f"observations-step-{rate:g}.json").write_text(
            json.dumps(step_obs, ensure_ascii=False, indent=2) + "\n")
        metrics = _step_metrics(merged, step_obs)
        steps.append(metrics)
        detect_knee(steps, rate)   # 逐档即时判定，决定是否继续升压
        print(f"[ok] 档位 {rate:g}：achieved={metrics['achieved_rps']} "
              f"err={metrics['error_rate']:.2%} "
              f"p95={metrics['p95_corrected_ms']}ms(corrected) "
              f"p99={metrics['p99_corrected_ms']}ms "
              f"gen_cpu={metrics['generator_cpu_pct']}%"
              + (f" ⚠ {metrics['knee_reasons']}" if metrics["knee"] else ""))
        if metrics["knee"] and not args.continue_past_knee:
            print("[stop] 已触拐点，停止升压（--continue-past-knee 可继续观察崩溃形态）")
            break

    knee = detect_knee(steps, args.target_rps)
    target = args.target_rps
    validated = knee.get("validated_max_rps") or 0
    status = decide_status(knee, target)
    verdict = {
        "status": status,
        "capacity_target_rps": target,
        "knee_at_rate": knee["knee_at_rate"],
        "validated_max_rps": validated,
        "invalid_steps": knee.get("invalid_steps") or [],
        "untrusted_knee_rates": knee.get("untrusted_knee_rates") or [],
        "reasons": knee["reasons"],
        "notes": knee["notes"],
        "meets_target": status == "meets",
        "margin_x": round(validated / target, 2) if target else None,
    }
    summary = {
        "kind": "ladder",
        "tag": args.tag,
        "profile": f"ladder({args.tier})",
        "pool_tier": args.tier,
        "pool_origin": _rel(pool_path),
        "pool_usable_as_baseline": pool.pool_usable_as_baseline(payload),
        "pool_meta": payload.get("meta", {}),
        "host": args.host,
        "duration": args.step_duration,
        "started_at": started.isoformat(),
        "git_sha": _git_sha(),
        "hardware": {"platform": platform.platform(), "cpu_count": os.cpu_count()},
        "guard": "passed",
        "generator_context": ctx,
        "capacity_target": {**CAPACITY_TARGET, "target_rps": target},
        "steps": steps,
        "knee": knee,
        "verdict": verdict,
        "observations_file": "observations-step-*.json",
    }
    (archive / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(f"[ok] 阶梯归档 {_rel(archive)}")
    if status == "meets":
        print(f"[ok] 达到容量目标 {target:g} RPS（有效验证上限 {validated:g} RPS，未见服务端拐点）")
    elif status == "below":
        print(f"[FAIL] 拐点 {knee['knee_at_rate']:g} RPS 低于目标 {target:g} RPS"
              f"：{verdict['reasons']}")
    elif status == "environment_limited":
        print(f"[warn] 档位 {verdict['untrusted_knee_rates']} 出现延迟抬升但服务端 CPU 很低："
              "判为压测环境限制，**不能**认定容量上限——需独立压测机 + 服务端侧延迟分解")
    else:
        print(f"[warn] 未验证到目标 {target:g} RPS：有效档位最高 {validated:g} RPS；"
              f"档位 {verdict['invalid_steps']} 因压测机饱和无效——需独立压测机重跑")
    cmd_report(argparse.Namespace(archive=str(archive)))
    return 0 if status == "meets" else 1


# ---------------------------------------------------------------- compare

def series_from_ladder(summary: dict) -> dict[str, dict]:
    """阶梯归档的对比序列：键为档位（目标 RPS），值为该档指标。"""
    out: dict[str, dict] = {}
    for step in summary.get("steps") or []:
        rate = step.get("target_rps")
        if rate is None:
            continue
        out[f"{rate:g} RPS"] = {
            "p95": step.get("p95_corrected_ms"),
            "p99": step.get("p99_corrected_ms"),
            "poll_p95": step.get("poll_p95_corrected_ms"),
            "error_rate": step.get("error_rate"),
            "throughput": step.get("achieved_rps"),
            "trustworthy": step.get("knee_trustworthy", True)
            and not step.get("invalid_generator_bound"),
        }
    return out


def series_from_locust(summary: dict, endpoint_rows: list[dict]) -> dict[str, dict]:
    """单档归档的对比序列：键为归一化端点名（含 Aggregated）。"""
    out: dict[str, dict] = {}
    locust = summary.get("locust") or {}
    out["Aggregated"] = {
        "p95": locust.get("p95_ms"), "p99": None,
        "poll_p95": next((v.get("p95_ms") for k, v in (locust.get("by_name") or {}).items()
                          if "judgments" in k), None),
        "error_rate": locust.get("error_rate"),
        "throughput": None, "trustworthy": True,
    }
    for item in _aggregate_endpoints(endpoint_rows):
        out[item["name"]] = {
            "p95": item["p95"], "p99": item["p99"],
            "poll_p95": None, "error_rate": (item["failures"] / item["requests"]
                                             if item["requests"] else 0.0),
            "throughput": item["rps"], "trustworthy": True,
        }
    return out


def compatibility(cand: dict, base: dict) -> list[str]:
    """对比前提校验：口径不同的两次运行不可比较（宁可拒绝，不要产出假结论）。"""
    problems: list[str] = []
    cand_kind = cand.get("kind", "locust")
    base_kind = base.get("kind", "locust")
    if cand_kind != base_kind:
        problems.append(f"归档类型不同：候选 {cand_kind} vs 基线 {base_kind}")
    for field, label in (("profile", "profile"), ("pool_tier", "池档位")):
        if cand.get(field) != base.get(field):
            problems.append(f"{label} 不同：{cand.get(field)} vs {base.get(field)}")
    cand_scale = ((cand.get("pool_meta") or {}).get("scale") or {}).get("judgments_total")
    base_scale = ((base.get("pool_meta") or {}).get("scale") or {}).get("judgments_total")
    if cand_scale and base_scale and cand_scale != base_scale:
        problems.append(f"数据规模不同：{cand_scale} vs {base_scale} 行（缓存命中率不可比）")
    cand_cpu = (cand.get("hardware") or {}).get("cpu_count")
    base_cpu = (base.get("hardware") or {}).get("cpu_count")
    if cand_cpu and base_cpu and cand_cpu != base_cpu:
        problems.append(f"硬件核数不同：{cand_cpu} vs {base_cpu}")
    return problems


def build_comparison(cand: dict, base: dict, cand_series: dict[str, dict],
                     base_series: dict[str, dict],
                     threshold: float = 0.30) -> dict:
    """逐键对比并标记劣化（p95 劣化 > threshold；错误率同时看绝对与相对增幅）。

    纯函数：不读文件、不打印，便于单测与自校验（同一份自比必须报 0%）。
    """
    rows: list[dict] = []
    for key in sorted(set(cand_series) | set(base_series)):
        c = cand_series.get(key)
        b = base_series.get(key)
        if b is None:
            rows.append({"key": key, "status": "added", "note": "基线无此档位/端点"})
            continue
        if c is None:
            rows.append({"key": key, "status": "removed", "note": "本次缺少该档位/端点"})
            continue
        row = {"key": key, "status": "ok", "trustworthy": c.get("trustworthy", True)}
        regressions: list[str] = []
        for field, label in (("p95", "p95"), ("p99", "p99"), ("poll_p95", "轮询 p95")):
            cv, bv = c.get(field), b.get(field)
            if cv is None or bv in (None, 0):
                continue
            delta = cv / bv - 1
            row[f"{field}_delta"] = round(delta, 3)
            if delta > threshold:
                regressions.append(f"{label} 劣化 {delta:+.1%}（{bv} → {cv}）")
        ce, be = c.get("error_rate"), b.get("error_rate")
        if ce is not None and be is not None:
            row["error_rate_delta"] = round(ce - be, 4)
            if ce > be + 0.005 and (be == 0 or ce > 2 * be):
                regressions.append(f"错误率上升 {be:.2%} → {ce:.2%}")
        ct, bt = c.get("throughput"), b.get("throughput")
        if ct and bt:
            delta = ct / bt - 1
            row["throughput_delta"] = round(delta, 3)
            if delta < -threshold:
                regressions.append(f"吞吐下降 {delta:+.1%}（{bt} → {ct}）")
        if regressions:
            row["status"] = "regressed"
            row["regressions"] = regressions
            if not row["trustworthy"]:
                row["note"] = "该档位数据本身无效（压测机饱和/环境限制），劣化仅供参考"
        rows.append(row)
    regressed = [r for r in rows if r["status"] == "regressed"]
    return {
        "threshold": threshold,
        "candidate": {"tag": cand.get("tag"), "started_at": cand.get("started_at"),
                      "git_sha": cand.get("git_sha")},
        "baseline": {"tag": base.get("tag"), "started_at": base.get("started_at"),
                     "git_sha": base.get("git_sha")},
        "rows": rows,
        "regressions": len(regressed),
        "regressed_keys": [r["key"] for r in regressed],
        "verdict": "regressed" if regressed else "clean",
    }


def _archive_summary(path: Path) -> dict:
    summary_path = path / "summary.json"
    if not summary_path.exists():
        raise SystemExit(f"[FAIL] {path} 不是归档目录（缺 summary.json）")
    return json.loads(summary_path.read_text())


def cmd_compare(args) -> int:
    cand_dir = Path(args.candidate)
    if not cand_dir.is_absolute():
        cand_dir = ROOT / cand_dir
    base_dir = Path(args.baseline)
    if not base_dir.is_absolute():
        base_dir = ROOT / base_dir
    cand, base = _archive_summary(cand_dir), _archive_summary(base_dir)

    problems = compatibility(cand, base)
    if problems and not args.force:
        print("[FAIL] 两次运行口径不同，对比结论不可用：")
        for problem in problems:
            print(f"  - {problem}")
        print("（确要对比请传 --force，归档会标注口径不一致）")
        return 2

    if cand.get("kind") == "ladder":
        cand_series = series_from_ladder(cand)
        base_series = series_from_ladder(base)
    else:
        cand_series = series_from_locust(cand, _read_csv(cand_dir / "locust_stats.csv"))
        base_series = series_from_locust(base, _read_csv(base_dir / "locust_stats.csv"))

    comparison = build_comparison(cand, base, cand_series, base_series, args.threshold)
    comparison["compatibility_problems"] = problems
    (cand_dir / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2) + "\n")

    print(f"对比：{comparison['candidate']['tag']} ← {comparison['baseline']['tag']}"
          f"（阈值 {args.threshold:.0%}）")
    for row in comparison["rows"]:
        if row["status"] == "regressed":
            for line in row["regressions"]:
                print(f"  [FAIL] {row['key']}: {line}")
            if row.get("note"):
                print(f"         note: {row['note']}")
        elif row["status"] in ("added", "removed"):
            print(f"  [skip] {row['key']}: {row['note']}")
    if comparison["verdict"] == "clean":
        print(f"[ok] 无劣化项（共 {len(comparison['rows'])} 项对比）")
    else:
        print(f"[FAIL] {comparison['regressions']} 项劣化：{comparison['regressed_keys']}")
    print(f"[ok] 对比结果写入 {_rel(cand_dir / 'comparison.json')}")
    cmd_report(argparse.Namespace(archive=str(cand_dir)))
    return 0 if comparison["verdict"] == "clean" else 1


# ---------------------------------------------------------------- report

# 调性沿用 docs/design-tone.md：冷灰炭底 + 琥珀作唯一强调色（这里琥珀只标
# 「需要看的东西」= 未达标项），数字全部等宽，禁止红色警报横幅与发光。
_REPORT_CSS = """
:root {
  --bg: #0e1116; --panel: #141920; --panel-2: #181f28;
  --line: rgba(255,255,255,.07); --ink: #e6e9ef; --muted: #8b93a1;
  --amber: #f0b429; --amber-hi: #ffd166;
  --font-ui: -apple-system, "SF Pro Display", "PingFang SC", sans-serif;
  --font-mono: "SF Mono", "JetBrains Mono", Menlo, monospace;
}
* { box-sizing: border-box; }
body { margin: 0; padding: 40px 32px 64px; background: var(--bg); color: var(--ink);
  font-family: var(--font-ui); font-size: 14px; line-height: 1.55; }
.wrap { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; letter-spacing: .01em; }
h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase;
  letter-spacing: .08em; margin: 36px 0 12px; }
.sub { color: var(--muted); font-family: var(--font-mono); font-size: 12px; }
.verdict { display: flex; align-items: baseline; gap: 12px; margin: 20px 0 8px;
  padding: 14px 16px; background: var(--panel); border: 1px solid var(--line);
  border-left: 3px solid var(--muted); border-radius: 3px; }
.verdict.bad { border-left-color: var(--amber); }
.verdict .tag { font-size: 12px; letter-spacing: .06em; text-transform: uppercase;
  color: var(--muted); }
.verdict.bad .tag { color: var(--amber); }
.verdict .note { color: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px; margin-top: 18px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 3px;
  padding: 12px 14px; }
.card .k { color: var(--muted); font-size: 11px; text-transform: uppercase;
  letter-spacing: .06em; }
.card .v { font-family: var(--font-mono); font-size: 20px; margin-top: 6px; }
.card.flag .v { color: var(--amber); }
table { width: 100%; border-collapse: collapse; background: var(--panel);
  border: 1px solid var(--line); border-radius: 3px; }
th, td { padding: 8px 12px; text-align: right; border-bottom: 1px solid var(--line);
  font-family: var(--font-mono); font-size: 12px; white-space: nowrap; }
th { color: var(--muted); font-weight: 500; text-transform: uppercase; font-size: 11px;
  letter-spacing: .05em; }
th:first-child, td:first-child { text-align: left; font-family: var(--font-ui); }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: var(--panel-2); }
.fail td { color: var(--amber); }
dl { display: grid; grid-template-columns: 200px 1fr; gap: 6px 16px; margin: 0;
  font-family: var(--font-mono); font-size: 12px; }
dt { color: var(--muted); }
dd { margin: 0; overflow-wrap: anywhere; }
pre { background: var(--panel); border: 1px solid var(--line); border-radius: 3px;
  padding: 12px 14px; overflow-x: auto; font-family: var(--font-mono); font-size: 11px;
  color: var(--muted); margin: 0; }
.spark { display: flex; gap: 24px; flex-wrap: wrap; }
.spark figure { margin: 0; background: var(--panel); border: 1px solid var(--line);
  border-radius: 3px; padding: 12px 14px; }
.spark figcaption { color: var(--muted); font-size: 11px; text-transform: uppercase;
  letter-spacing: .06em; margin-bottom: 6px; }
.foot { color: var(--muted); font-size: 12px; margin-top: 40px; padding-top: 16px;
  border-top: 1px solid var(--line); }
a { color: var(--ink); text-decoration: none; border-bottom: 1px solid var(--line); }
a:hover { border-bottom-color: var(--amber); }
"""

_STATUS_TAG = {
    "meets": "✓ 达到容量目标",
    "below": "⚠ 未达容量目标",
    "indeterminate": "⚠ 未验证到目标（受压测机限制）",
    "environment_limited": "⚠ 拐点不可信（疑压测环境限制）",
}

_AMBER = "#f0b429"
_MUTED = "#8b93a1"


def _read_csv(path: Path) -> list[dict]:
    import csv

    if not path.exists():
        return []
    with path.open() as handle:
        return list(csv.DictReader(handle))


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_name(name: str) -> str:
    """把逐地址/逐页的请求名归并，避免报告里出现上千行。

    旧归档的 locustfile 未给 subgraph 起稳定名（每个地址一行），这里兜底归并；
    新档已用 `name=` 固定，归并是幂等的。
    """
    import re

    name = re.sub(r"/addresses/[^/]+/subgraph", "/addresses/[addr]/subgraph", name)
    name = re.sub(r"(cases\?page=)\d+", r"\1N", name)
    name = re.sub(r"/judgments/[0-9a-f-]{36}", "/judgments/[id]", name)
    return name


def _aggregate_endpoints(rows: list[dict]) -> list[dict]:
    """按归一化名称聚合；p95 取组内最大（保守口径，宁高不低）。

    排除 `warmup:*` 与 `Aggregated`：前者是 VU 启动期的一次性动作（登录 bcrypt
    ~210ms），混进稳态统计会把聚合 p95 抬高、也会在回归对比里制造假劣化；
    后者是 locust 自算的行，重复计入会翻倍。
    """
    groups: dict[str, dict] = {}
    for row in rows:
        name = (row.get("Name") or "").strip()
        if not name or name == "Aggregated" or name.startswith("warmup:"):
            continue
        key = _normalize_name(name)
        item = groups.setdefault(key, {"name": key, "requests": 0, "failures": 0,
                                       "p95": 0.0, "p99": 0.0, "max": 0.0, "rps": 0.0})
        item["requests"] += int(_num(row.get("Request Count")))
        item["failures"] += int(_num(row.get("Failure Count")))
        item["p95"] = max(item["p95"], _num(row.get("95%")))
        item["p99"] = max(item["p99"], _num(row.get("99%")))
        item["max"] = max(item["max"], _num(row.get("Max Response Time")))
        item["rps"] += _num(row.get("Requests/s"))
    return sorted(groups.values(), key=lambda i: -i["requests"])


def _sparkline(points: list[float], color: str = _MUTED) -> str:
    """内联 SVG 折线（无 JS、无外部资源）：压测期间的趋势用一眼扫过就够。"""
    if len(points) < 2:
        return "<span class='sub'>（无历史采样）</span>"
    width, height, pad = 320, 56, 4
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1.0
    step = (width - 2 * pad) / (len(points) - 1)
    coords = " ".join(
        f"{pad + i * step:.1f},{height - pad - (v - lo) / span * (height - 2 * pad):.1f}"
        for i, v in enumerate(points))
    return (
        f"<svg width='{width}' height='{height}' viewBox='0 0 {width} {height}' "
        f"role='img' aria-label='trend'>"
        f"<polyline points='{coords}' fill='none' stroke='{color}' stroke-width='1.5'/>"
        f"<text x='{pad}' y='{height - 1}' fill='{_MUTED}' font-size='9' "
        f"font-family='monospace'>{lo:.0f}</text>"
        f"<text x='{width - pad}' y='{height - 1}' fill='{_MUTED}' font-size='9' "
        f"text-anchor='end' font-family='monospace'>{hi:.0f}</text></svg>")


def _render_comparison(comparison: dict) -> str:
    """回归对比区块（spec §3.1）：p95 劣化 > 阈值即标出。"""
    import html

    esc = lambda v: html.escape(str(v), quote=True)  # noqa: E731
    rows = comparison.get("rows") or []
    if not rows:
        return ""

    def cell(value, invert=False):
        if value is None:
            return "<td>—</td>"
        bad = (value > 0) if not invert else (value < 0)
        mark = f"{value:+.1%}"
        return f"<td>{mark}</td>" if not bad else f"<td>{mark} ⚠</td>"

    body = "".join(
        f"<tr class='{'fail' if row['status'] == 'regressed' else ''}'>"
        f"<td>{esc(row['key'])}</td>"
        + cell(row.get("p95_delta")) + cell(row.get("p99_delta"))
        + cell(row.get("poll_p95_delta")) + cell(row.get("error_rate_delta"))
        + cell(row.get("throughput_delta"), invert=True)
        + f"<td>{esc(row['status'])}</td></tr>"
        for row in rows)

    note = ""
    if comparison.get("compatibility_problems"):
        note = ("<p class='sub'>⚠ 口径不一致（--force 对比）："
                + esc("；".join(comparison["compatibility_problems"])) + "</p>")
    verdict = ("无劣化项" if comparison.get("verdict") == "clean"
               else f"{comparison.get('regressions')} 项劣化")
    return (f"<h2>回归对比（{esc(comparison.get('baseline', {}).get('tag'))} → "
            f"{esc(comparison.get('candidate', {}).get('tag'))}，阈值 "
            f"{comparison.get('threshold', 0):.0%}）</h2>{note}"
            "<table><thead><tr><th>档位 / 端点</th><th>p95 Δ</th><th>p99 Δ</th>"
            "<th>轮询 p95 Δ</th><th>错误率 Δ</th><th>吞吐 Δ</th><th>判定</th></tr></thead>"
            f"<tbody>{body}</tbody></table>"
            f"<p class='sub'>结论：{esc(verdict)}</p>")


def render_html(summary: dict, observations: dict, endpoint_rows: list[dict],
                history_rows: list[dict], failures_rows: list[dict],
                comparison: dict | None = None) -> str:
    """归档 → 自包含 HTML。阶梯归档走 _render_ladder，单档走 _render_locust。"""
    if summary.get("kind") == "ladder":
        return _render_ladder(summary, observations, {}, comparison)
    return _render_locust(summary, observations, endpoint_rows, history_rows,
                          failures_rows, comparison)


def _render_locust(summary: dict, observations: dict, endpoint_rows: list[dict],
                   history_rows: list[dict], failures_rows: list[dict],
                   comparison: dict | None = None) -> str:
    """单档（Locust 闭环）归档的渲染。"""
    import html

    esc = lambda v: html.escape(str(v), quote=True)  # noqa: E731
    locust = summary.get("locust") or {}
    tier = summary.get("pool_tier", "?")
    poll = next((v for k, v in (locust.get("by_name") or {}).items()
                 if "judgments" in k), {})
    error_rate = locust.get("error_rate")
    poll_p95 = poll.get("p95_ms")

    checks: list[tuple[str, bool, str]] = []
    if error_rate is not None:
        checks.append(("错误率 < 1%", error_rate < 0.01, f"{error_rate:.2%}"))
    if tier == "hot" and poll_p95 is not None:
        checks.append(("热档轮询 p95 ≤ 300ms", poll_p95 <= 300, f"{poll_p95}ms"))
    if summary.get("pool_usable_as_baseline") is not None:
        checks.append(("池满足基线口径（热/冷均非空）",
                       bool(summary["pool_usable_as_baseline"]),
                       "usable" if summary["pool_usable_as_baseline"] else "degraded"))
    bad = [name for name, ok, _ in checks if not ok]

    def card(label, value, flag=False):
        return (f"<div class='card{' flag' if flag else ''}'>"
                f"<div class='k'>{esc(label)}</div>"
                f"<div class='v'>{esc(value)}</div></div>")

    cards = "".join([
        card("总请求", locust.get("total_requests", 0)),
        card("失败", locust.get("total_failures", 0),
             bool(locust.get("total_failures"))),
        card("错误率", "n/a" if error_rate is None else f"{error_rate:.2%}",
             error_rate is not None and error_rate >= 0.01),
        card("聚合 p95", "n/a" if locust.get("p95_ms") is None
             else f"{locust['p95_ms']}ms"),
        card(f"轮询 p95 ({tier})", "n/a" if poll_p95 is None else f"{poll_p95}ms",
             tier == "hot" and poll_p95 is not None and poll_p95 > 300),
        card("并发 / 时长", f"{summary.get('vu', '?')} VU / {summary.get('duration', '?')}"),
    ])

    check_rows = "".join(
        f"<tr class='{'' if ok else 'fail'}'><td>{esc(name)}</td>"
        f"<td>{'✓ 达标' if ok else '⚠ 未达标'}</td><td>{esc(detail)}</td></tr>"
        for name, ok, detail in checks)

    endpoint_table = "".join(
        f"<tr class='{'fail' if item['failures'] else ''}'>"
        f"<td>{esc(item['name'])}</td><td>{item['requests']}</td>"
        f"<td>{item['failures']}</td><td>{item['p95']:.0f}</td>"
        f"<td>{item['p99']:.0f}</td><td>{item['max']:.0f}</td>"
        f"<td>{item['rps']:.1f}</td></tr>"
        for item in _aggregate_endpoints(endpoint_rows))

    failure_table = "".join(
        f"<tr class='fail'><td>{esc(row.get('Name'))}</td>"
        f"<td>{esc(row.get('Error'))}</td><td>{esc(row.get('Occurrences'))}</td></tr>"
        for row in failures_rows)

    rps_series = [_num(r.get("Requests/s")) for r in history_rows
                  if (r.get("Name") or "") == "Aggregated"]
    p95_series = [_num(r.get("95%")) for r in history_rows
                  if (r.get("Name") or "") == "Aggregated"
                  and (r.get("95%") or "N/A") != "N/A"]

    meta = summary.get("pool_meta") or {}
    scale = meta.get("scale") or {}
    obs = observations or {}
    meta_rows = "".join(
        f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in [
            ("tag", summary.get("tag")),
            ("profile / tier", f"{summary.get('profile')} / {tier}"),
            ("host", summary.get("host")),
            ("started_at", summary.get("started_at")),
            ("git_sha", summary.get("git_sha")),
            ("环境守卫", summary.get("guard")),
            ("mock_scenario", summary.get("mock_scenario") or "—"),
            ("硬件", summary.get("hardware")),
            ("数据规模", scale or "—"),
            ("池规模", meta.get("sizes") or "—"),
            ("池来源", summary.get("pool_origin")),
            ("观测项文件", summary.get("observations_file")),
        ] if v not in (None, ""))

    obs_rows = "".join(
        f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in [
            ("队列深度", obs.get("queues")),
            ("analysis_spans", obs.get("analysis_spans")),
            ("PG 连接数", obs.get("pg_activity")),
            ("PG 等待事件", obs.get("pg_wait_events")),
            ("Redis clients", (obs.get("redis_clients") or "").splitlines()[:6]),
            ("docker stats", (obs.get("docker_stats") or "").splitlines()),
        ] if v not in (None, "", []))

    verdict_class = "bad" if bad else ""
    verdict_tag = "⚠ 存在未达标项" if bad else "✓ 全部达标"
    verdict_note = ("需调查：" + "；".join(bad)) if bad else "本档位未见未达标项"

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>压测报告 · {esc(summary.get('tag'))}</title>
<style>{_REPORT_CSS}</style></head>
<body><div class="wrap">
<h1>压测报告 · {esc(summary.get('tag'))}</h1>
<div class="sub">{esc(summary.get('profile'))} / {esc(tier)} · {esc(summary.get('started_at'))}
 · git {esc(summary.get('git_sha'))}</div>

<div class="verdict {verdict_class}"><span class="tag">{verdict_tag}</span>
  <span class="note">{esc(verdict_note)}</span></div>

<div class="cards">{cards}</div>

<h2>达标判定</h2>
<table><thead><tr><th>口径</th><th>结果</th><th>实测</th></tr></thead>
<tbody>{check_rows or "<tr><td>—</td><td>—</td><td>无可判定项</td></tr>"}</tbody></table>

<h2>趋势（稳态期）</h2>
<div class="spark">
  <figure><figcaption>Requests/s</figcaption>{_sparkline(rps_series, _AMBER)}</figure>
  <figure><figcaption>p95 (ms)</figcaption>{_sparkline(p95_series, _MUTED)}</figure>
</div>

<h2>端点明细（按归一化路径聚合，p95 取组内最大）</h2>
<table><thead><tr><th>端点</th><th>请求</th><th>失败</th><th>p95 (ms)</th>
<th>p99 (ms)</th><th>max (ms)</th><th>req/s</th></tr></thead>
<tbody>{endpoint_table or "<tr><td>—</td><td colspan='6'>无数据</td></tr>"}</tbody></table>

<h2>失败明细</h2>
<table><thead><tr><th>端点</th><th>错误</th><th>次数</th></tr></thead>
<tbody>{failure_table or "<tr><td>—</td><td colspan='2'>无失败</td></tr>"}</tbody></table>

{_render_comparison(comparison) if comparison else ''}

<h2>归档元数据</h2><dl>{meta_rows}</dl>

<h2>服务端观测项</h2><dl>{obs_rows}</dl>

<p class="foot">口径：热/冷两档不可合并统计；端点表已排除 warmup（VU 启动期一次性动作）
与 locust 自算的 Aggregated 行；本档位为
{esc(summary.get('vu'))} VU / {esc(summary.get('duration'))}，不可外推到更高并发或更大数据量。
冷热两档在数据量不足以撑出缓存差异时读数会相同——那是数据规模的限制，不是缓存无所谓的证据。
生成方式：<code>uv run python tests/performance/run_stress.py report</code>
</p>
</div></body></html>
"""


def _render_ladder(summary: dict, observations: dict, step_detail: dict,
                   comparison: dict | None = None) -> str:
    """阶梯归档渲染：拐点结论 + 逐档表 + 校正延迟曲线（spec §3.5/§3.6）。"""
    import html

    esc = lambda v: html.escape(str(v), quote=True)  # noqa: E731
    steps = summary.get("steps") or []
    verdict = summary.get("verdict") or {}
    knee = summary.get("knee") or {}
    target = verdict.get("capacity_target_rps")
    meets = bool(verdict.get("meets_target"))
    status = verdict.get("status") or ("meets" if meets else "below")
    validated = verdict.get("validated_max_rps") or 0
    if status == "meets":
        note_text = (f"有效验证到 {validated:g} RPS（目标 {target:g} RPS），未见服务端拐点")
    elif status == "below":
        note_text = f"拐点 {knee.get('knee_at_rate'):g} RPS 低于目标 {target:g} RPS"
    elif status == "environment_limited":
        note_text = ("档位 " + "/".join(f"{r:g}" for r in verdict.get("untrusted_knee_rates") or [])
                     + f" 出现延迟抬升，但服务端 CPU 很低——判为压测环境限制，"
                       f"不能据此认定容量上限（目标 {target:g} RPS 仍未验证）")
    else:
        note_text = (f"未验证到目标 {target:g} RPS：有效档位最高 {validated:g} RPS，"
                     f"档位 {verdict.get('invalid_steps')} 因压测机饱和无效")
    gen_ctx = summary.get("generator_context") or {}

    def card(label, value, flag=False):
        return (f"<div class='card{' flag' if flag else ''}'>"
                f"<div class='k'>{esc(label)}</div>"
                f"<div class='v'>{esc(value)}</div></div>")

    gen_peak = max([s.get("generator_cpu_pct") or 0 for s in steps] or [0])
    cards = "".join([
        card("容量目标", f"{target:g} RPS" if target else "n/a"),
        card("有效验证上限", f"{verdict.get('validated_max_rps', 0):g} RPS",
             not meets),
        card("拐点", f"{knee['knee_at_rate']:g} RPS" if knee.get("knee_at_rate")
             else "未触", bool(knee.get("knee_at_rate"))),
        card("余量倍数", f"{verdict.get('margin_x')}×" if verdict.get("margin_x") else "n/a",
             not meets),
        card("生成器峰值 CPU", f"{gen_peak:g}%", gen_peak >= 60),
        card("数据规模", (summary.get("pool_meta") or {}).get("scale", {}).get(
            "judgments_total", "?")),
    ])

    def verdict_cell(step) -> str:
        if step.get("knee"):
            return "⚠ 触拐点"
        if step.get("invalid_generator_bound"):
            return "✕ 无效（压测机饱和）"
        if step.get("knee") and not step.get("knee_trustworthy"):
            return "⚠ 抬升但疑环境限制"
        return "✓ 通过"

    def row(step):
        flagged = step.get("knee") or step.get("invalid_generator_bound")
        err = step.get("error_rate")
        return (
            f"<tr class='{'fail' if flagged else ''}'>"
            f"<td>{step['target_rps']:g}</td><td>{step.get('achieved_rps')}</td>"
            f"<td>{step.get('p95_tool_ms')}</td><td>{step.get('p95_corrected_ms')}</td>"
            f"<td>{step.get('p99_corrected_ms')}</td>"
            f"<td>{step.get('poll_p95_corrected_ms')}</td>"
            f"<td>{step.get('poll_p99_corrected_ms')}</td>"
            f"<td>{0 if err is None else err:.2%}</td>"
            f"<td>{step.get('generator_cpu_pct')}</td>"
            f"<td>{step.get('server_cpu_pct')}</td>"
            f"<td>{verdict_cell(step)}</td></tr>")

    ladder_rows = "".join(row(s) for s in steps) or \
        "<tr><td colspan='10'>无有效档位</td></tr>"

    endpoints = ((step_detail.get("by_endpoint") or {}) if step_detail else {})
    endpoint_table = "".join(
        f"<tr class='{'fail' if item.get('failures') else ''}'>"
        f"<td>{esc(name)}</td><td>{item.get('requests')}</td>"
        f"<td>{item.get('failures')}</td>"
        f"<td>{(item.get('corrected_ms') or {}).get('p50')}</td>"
        f"<td>{(item.get('corrected_ms') or {}).get('p95')}</td>"
        f"<td>{(item.get('corrected_ms') or {}).get('p99')}</td></tr>"
        for name, item in sorted(endpoints.items(),
                                 key=lambda kv: -(kv[1].get("requests") or 0)))

    poll_p95_series = [s.get("poll_p95_corrected_ms") or 0 for s in steps]
    err_series = [(s.get("error_rate") or 0) * 100 for s in steps]
    obs = observations or {}
    obs_rows = "".join(
        f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in [
            ("队列深度（末档）", obs.get("queues")),
            ("analysis_spans", obs.get("analysis_spans")),
            ("PG 连接数", obs.get("pg_activity")),
            ("PG 等待事件", obs.get("pg_wait_events")),
            ("docker stats（末档）", (obs.get("docker_stats") or "").splitlines()),
            ("压测机", {**gen_ctx, "loadavg_after":
                        (steps[-1] if steps else {}).get("loadavg_after")}),
        ] if v not in (None, "", []))

    reasons = "".join(f"<li>{esc(r)}</li>" for r in (knee.get("reasons") or []))
    reasons += "".join(
        f"<li>{esc(r)}</li>" for step in steps
        for r in (step.get("invalid_reasons") or []))
    if not reasons:
        reasons = "<li>有效档位内未出现非线性抬升 / 错误率抬头 / 报价跟不上</li>"

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>阶梯压测报告 · {esc(summary.get('tag'))}</title>
<style>{_REPORT_CSS}</style></head>
<body><div class="wrap">
<h1>阶梯压测报告 · {esc(summary.get('tag'))}</h1>
<div class="sub">开放模型（固定到达率）· {esc(summary.get('profile'))} ·
 {esc(summary.get('started_at'))} · git {esc(summary.get('git_sha'))}</div>

<div class="verdict {'bad' if not meets else ''}">
  <span class="tag">{_STATUS_TAG.get(status, status)}</span>
  <span class="note">{esc(note_text)}</span>
</div>

<div class="cards">{cards}</div>

<h2>拐点判定（spec §3.5）</h2>
<ul>{reasons}</ul>
<p class="sub">判定用校正延迟（从计划发出时刻计，含排队等待）；工具口径会系统性低估尾延迟。
目标推导：{esc((summary.get('capacity_target') or {}).get('derivation'))}</p>

<h2>逐档结果</h2>
<table><thead><tr><th>目标 RPS</th><th>实测 RPS</th><th>p95 工具</th><th>p95 校正</th>
<th>p99 校正</th><th>轮询 p95</th><th>轮询 p99</th><th>错误率</th><th>生成器 CPU</th><th>服务端 CPU</th>
<th>判定</th></tr></thead><tbody>{ladder_rows}</tbody></table>

<h2>趋势（按档位）</h2>
<div class="spark">
  <figure><figcaption>轮询 p95 校正 (ms)</figcaption>
    {_sparkline(poll_p95_series, _AMBER)}</figure>
  <figure><figcaption>错误率 (%)</figcaption>{_sparkline(err_series, _MUTED)}</figure>
</div>

<h2>端点明细（最高档，校正分位数）</h2>
<table><thead><tr><th>端点</th><th>请求</th><th>失败</th><th>p50</th><th>p95</th>
<th>p99</th></tr></thead>
<tbody>{endpoint_table or "<tr><td>—</td><td colspan='5'>无数据</td></tr>"}</tbody></table>

{_render_comparison(comparison) if comparison else ''}

<h2>观测与偏差</h2><dl>{obs_rows}</dl>

<p class="foot">口径：热/冷两档不可合并统计；端点表已排除 warmup（VU 启动期一次性动作）
与 locust 自算的 Aggregated 行；校正延迟含排队等待，判定以它为准。
{'<strong>本次压测机与被测系统同机，资源观测不可作为容量依据</strong>；'
 '生产级判定需把生成器放到独立机器并确认其 CPU 余量。' if gen_ctx.get('co_located_with_sut') else ''}
生成方式：<code>uv run python tests/performance/run_stress.py ladder ...</code>
</p>
</div></body></html>
"""


def cmd_report(args) -> int:
    archives = ([Path(args.archive)] if args.archive
                else sorted(p for p in OUTPUT_ROOT.iterdir()
                            if p.is_dir() and (p / "summary.json").exists()))
    if not archives:
        print("[FAIL] 没有可渲染的归档（先跑 baseline）")
        return 1
    written = []
    for archive in archives:
        summary_path = archive / "summary.json"
        if not summary_path.exists():
            print(f"[skip] {_rel(archive)} 无 summary.json")
            continue
        summary = json.loads(summary_path.read_text())
        comparison = None
        cmp_path = archive / "comparison.json"
        if cmp_path.exists():
            comparison = json.loads(cmp_path.read_text())
        if summary.get("kind") == "ladder":
            steps = summary.get("steps") or []
            detail = {}
            if steps:
                step_file = archive / f"step-{steps[-1]['target_rps']:g}.json"
                if step_file.exists():
                    detail = json.loads(step_file.read_text())
            obs = {}
            obs_files = sorted(archive.glob("observations-step-*.json"))
            if obs_files:
                obs = json.loads(obs_files[-1].read_text())
            html_text = _render_ladder(summary, obs, detail, comparison)
        else:
            observations = {}
            obs_path = archive / "observations.json"
            if obs_path.exists():
                observations = json.loads(obs_path.read_text())
            html_text = render_html(
                summary, observations,
                _read_csv(archive / "locust_stats.csv"),
                _read_csv(archive / "locust_stats_history.csv"),
                _read_csv(archive / "locust_failures.csv"), comparison)
        out = archive / "report.html"
        out.write_text(html_text)
        written.append(out)
        print(f"[ok] {_rel(out)}")
    if len(written) > 1 and not args.archive:
        links = "\n".join(
            f'<li><a href="{p.parent.name}/report.html">{p.parent.name}</a></li>'
            for p in written)
        index = OUTPUT_ROOT / "index.html"
        index.write_text(
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>压测归档</title><style>" + _REPORT_CSS + "</style></head>"
            "<body><div class='wrap'><h1>压测归档</h1>"
            f"<ul>{links}</ul></div></body></html>")
        print(f"[ok] {_rel(index)}")
    return 0


# ---------------------------------------------------------------- entry

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="stress-test single entry")
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("guard", help="环境守卫（fixture+mock）")
    g.add_argument("--allow-non-fixture", action="store_true")
    g.set_defaults(func=cmd_guard)

    p = sub.add_parser("pool", help="生成分层抽样 id 池")
    p.add_argument("--tag", required=True)
    p.add_argument("--hot", type=int, default=500)
    p.add_argument("--cold", type=int, default=500)
    p.add_argument("--addresses", type=int, default=200)
    p.add_argument("--hot-hours", type=int, default=1)
    p.set_defaults(func=cmd_pool)

    b = sub.add_parser("baseline", help="跑单档并归档（含观测项）")
    b.add_argument("--tag", required=True)
    b.add_argument("--profile", default="read",
                   choices=["read", "mixed", "anon"])
    b.add_argument("--tier", default="hot", choices=["hot", "cold"])
    b.add_argument("--vu", type=int, default=10)
    b.add_argument("--duration", default="60s")
    b.add_argument("--host", default=DEFAULT_HOST)
    b.add_argument("--pool", default=None)
    b.add_argument("--hot", type=int, default=500)
    b.add_argument("--cold", type=int, default=500)
    b.add_argument("--addresses", type=int, default=200)
    b.add_argument("--hot-hours", type=int, default=1)
    b.add_argument("--mock-scenario", default=None)
    b.add_argument("--expect-failure", action="store_true")
    b.add_argument("--allow-non-fixture", action="store_true")
    b.add_argument("--allow-derived-pool", action="store_true")
    b.set_defaults(func=cmd_baseline)

    c = sub.add_parser("cleanup", help="清理 STRESS 命名空间压测数据")
    c.set_defaults(func=cmd_cleanup)

    r = sub.add_parser("report", help="把归档渲染成自包含 HTML")
    r.add_argument("--archive", default=None,
                   help="指定归档目录；缺省渲染 output/stress 下全部归档并生成 index.html")
    r.set_defaults(func=cmd_report)

    ladder = sub.add_parser("ladder", help="开放模型阶梯升压 + 拐点判定")
    ladder.add_argument("--tag", required=True)
    ladder.add_argument("--rates", default="25,50,100,200,400",
                        help="目标到达率阶梯（逗号分隔，RPS）")
    ladder.add_argument("--step-duration", default="120s", help="每档测量时长")
    ladder.add_argument("--warmup", default="20s", help="每档预热（不计入统计）")
    ladder.add_argument("--tier", default="hot", choices=["hot", "cold"])
    ladder.add_argument("--host", default=DEFAULT_HOST)
    ladder.add_argument("--pool", default=None)
    ladder.add_argument("--target-rps", type=float,
                        default=CAPACITY_TARGET["target_rps"],
                        help="容量目标（spec §1.0，默认 250）")
    ladder.add_argument("--max-concurrency", type=int, default=256)
    ladder.add_argument("--generators", type=int, default=1,
                        help="生成器进程数（单进程在 ~250 RPS 触顶；多进程可探更高）")
    ladder.add_argument("--poisson", action="store_true", help="泊松到达（默认均匀）")
    ladder.add_argument("--continue-past-knee", action="store_true")
    ladder.add_argument("--hot", type=int, default=500)
    ladder.add_argument("--cold", type=int, default=500)
    ladder.add_argument("--addresses", type=int, default=200)
    ladder.add_argument("--hot-hours", type=int, default=1)
    ladder.add_argument("--allow-non-fixture", action="store_true")
    ladder.add_argument("--allow-derived-pool", action="store_true")
    ladder.set_defaults(func=cmd_ladder)
    compare = sub.add_parser("compare", help="与基线对比，标记 p95 劣化（默认阈值 0.30 = 30%%）")
    compare.add_argument("--candidate", required=True, help="本次归档目录")
    compare.add_argument("--baseline", required=True, help="基线归档目录")
    compare.add_argument("--threshold", type=float, default=0.30)
    compare.add_argument("--force", action="store_true",
                         help="口径不一致时仍强行对比（归档会标注）")
    compare.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
