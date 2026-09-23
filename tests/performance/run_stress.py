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


# ---------------------------------------------------------------- entry

def _pending(name: str) -> int:
    print(f"[skip] `{name}` 属 spec P1，未实现——不做静默空跑")
    return 2


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

    ladder = sub.add_parser("ladder", help="VU 阶梯（P1）")
    ladder.set_defaults(func=lambda a: _pending("ladder"))
    compare = sub.add_parser("compare", help="与基线对比（P1）")
    compare.set_defaults(func=lambda a: _pending("compare"))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
