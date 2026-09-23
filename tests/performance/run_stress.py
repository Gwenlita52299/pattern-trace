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
    """按归一化名称聚合；p95 取组内最大（保守口径，宁高不低）。"""
    groups: dict[str, dict] = {}
    for row in rows:
        name = (row.get("Name") or "").strip()
        if not name or name == "Aggregated":
            continue
        key = _normalize_name(name)
        item = groups.setdefault(key, {"name": key, "requests": 0, "failures": 0,
                                       "p95": 0.0, "max": 0.0, "rps": 0.0})
        item["requests"] += int(_num(row.get("Request Count")))
        item["failures"] += int(_num(row.get("Failure Count")))
        item["p95"] = max(item["p95"], _num(row.get("95%")))
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


def render_html(summary: dict, observations: dict, endpoint_rows: list[dict],
                history_rows: list[dict], failures_rows: list[dict]) -> str:
    """把归档渲染成自包含 HTML（纯函数，便于单测）。"""
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
        f"<td>{item['max']:.0f}</td><td>{item['rps']:.1f}</td></tr>"
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
<th>max (ms)</th><th>req/s</th></tr></thead>
<tbody>{endpoint_table or "<tr><td>—</td><td colspan='5'>无数据</td></tr>"}</tbody></table>

<h2>失败明细</h2>
<table><thead><tr><th>端点</th><th>错误</th><th>次数</th></tr></thead>
<tbody>{failure_table or "<tr><td>—</td><td colspan='2'>无失败</td></tr>"}</tbody></table>

<h2>归档元数据</h2><dl>{meta_rows}</dl>

<h2>服务端观测项</h2><dl>{obs_rows}</dl>

<p class="foot">口径：热/冷两档不可合并统计；本档位为
{esc(summary.get('vu'))} VU / {esc(summary.get('duration'))}，不可外推到更高并发或更大数据量。
冷热两档在数据量不足以撑出缓存差异时读数会相同——那是数据规模的限制，不是缓存无所谓的证据。
生成方式：<code>uv run python tests/performance/run_stress.py report</code>
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
        observations = {}
        obs_path = archive / "observations.json"
        if obs_path.exists():
            observations = json.loads(obs_path.read_text())
        html_text = render_html(
            summary, observations,
            _read_csv(archive / "locust_stats.csv"),
            _read_csv(archive / "locust_stats_history.csv"),
            _read_csv(archive / "locust_failures.csv"))
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

    r = sub.add_parser("report", help="把归档渲染成自包含 HTML")
    r.add_argument("--archive", default=None,
                   help="指定归档目录；缺省渲染 output/stress 下全部归档并生成 index.html")
    r.set_defaults(func=cmd_report)

    ladder = sub.add_parser("ladder", help="VU 阶梯（P1）")
    ladder.set_defaults(func=lambda a: _pending("ladder"))
    compare = sub.add_parser("compare", help="与基线对比（P1）")
    compare.set_defaults(func=lambda a: _pending("compare"))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
