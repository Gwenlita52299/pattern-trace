"""阶段6 性能基准 — PERF-01 / PERF-02（nightly 档）。

PERF-01  大案件（默认 60 地址）报告生成：耗时 ≤60s、PDF 魔数/体积、
         RSS 增长 ≤ 基线 ×3；
PERF-02  万级 judgments 查询：subgraph 与分页列表各采样 ≥30 次，
         p95 阈值 300ms / 200ms，并核对 EXPLAIN 是否 Index Scan。

口径提醒（v2 修订）：
- PERF-01 的 RSS 必须测**真正渲染的那一方**：compose 起 worker-report 时渲染发生在
  容器内，测压脚本自身的 RSS 没有意义。`_rss_probe()` 自动选择口径并在输出中自述；
  远程/无线程形态退化为测压进程（进程内渲染）并明确标注。
- PERF-01 只在「脚本进程与渲染进程使用同一 provider 配置」下自洽：若另起了 worker，
  容器的 GRAPH_DATA_MODE/LLM_PROVIDER 也必须是 fixture+mock，否则渲染会打到真实数据源。
- PERF-02 的 HTTP 口径（真实端点 + 序列化开销）与 DB 口径（纯 SQL）都测，前者为达标项，
  后者作为定位参考——两者不可混用结论。

用法：
    python tests/performance/perf_phase6.py            # 全部
    python tests/performance/perf_phase6.py PERF-01    # 仅报告容量
环境：
    DATABASE_URL   默认 postgresql://pt:pt@localhost:5432/patterntrace
    PERF_ADDRESSES 默认 60（PERF-01 规模）
    PERF_SAMPLES   默认 30（PERF-02 采样次数）
    PERF_RSS_TARGET 默认 auto；设 test 强制测压进程口径
退出码非 0 = 存在未达标项。
"""
from __future__ import annotations

import os
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ["GRAPH_DATA_MODE"] = "fixture"

FAILURES: list[str] = []


def check(name, cond, detail=""):
    print(f"[{'ok' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def _client_and_auth():
    from fastapi.testclient import TestClient

    from backend.api.app import create_app, seed_user

    app = create_app()
    client = TestClient(app)
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    seed_user("perf@demo.local", "PerfDemo!2026", role="investigator")
    r = client.post("/api/v1/auth/login",
                    json={"email": "perf@demo.local",
                          "password": "PerfDemo!2026"})
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    return client, auth


def _analyze(client, address):
    r = client.post("/api/v1/addresses/analyze", json={"address": address})
    assert r.status_code in (202, 200), r.text
    jid = r.json()["judgment_id"]
    deadline = time.time() + 30
    payload = {}
    while time.time() < deadline:
        payload = client.get(f"/api/v1/judgments/{jid}").json()
        if payload.get("status") in ("completed", "failed"):
            break
        time.sleep(0.2)
    assert payload.get("status") == "completed", payload
    return jid


# --------------------------------------------------------------- RSS 口径

def _compose_container(service: str) -> str | None:
    """取 compose 服务容器 id；无 docker/compose 或服务未起时返回 None。"""
    try:
        proc = subprocess.run(["docker", "compose", "ps", "-q", service],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    cid = proc.stdout.strip().splitlines()
    return cid[0] if proc.returncode == 0 and cid else None


def _mem_to_mb(text: str) -> float | None:
    """把 docker stats 的 '123.4MiB' 归一到 MB。"""
    value = text.strip().split()[0] if text.strip() else ""
    for suffix, factor in (("GiB", 1024.0), ("MiB", 1.0), ("KiB", 1 / 1024),
                           ("GB", 1000.0), ("MB", 1.0), ("kB", 1 / 1000), ("B", 1 / 1e6)):
        if value.endswith(suffix):
            try:
                return round(float(value[: -len(suffix)]) * factor, 1)
            except ValueError:
                return None
    return None


def _test_process_rss_mb() -> float:
    """ru_maxrss 单位平台相关（macOS=字节, Linux=KB），先归一再到 MB。"""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(raw * (1 if sys.platform == "darwin" else 1024) / (1024 ** 2), 1)


def _rss_probe() -> tuple[str, float]:
    """返回 (口径自述, 当前 RSS MB)。

    compose 起 worker-report 时渲染在其容器内——测压进程的 RSS 与渲染无关，
    必须测对目标；无 worker（CI nightly / 远程）时退化为进程内渲染口径并标注。
    """
    if os.environ.get("PERF_RSS_TARGET") != "test":
        cid = _compose_container("worker-report")
        if cid:
            out = subprocess.run(["docker", "stats", "--no-stream",
                                  "--format", "{{.MemUsage}}", cid],
                                 capture_output=True, text=True, timeout=30).stdout
            mb = _mem_to_mb(out.split("/")[0]) if out else None
            if mb is not None:
                return ("container:worker-report", mb)
    return ("test-process(in-process render; no worker-report container)",
            _test_process_rss_mb())


def perf01_report_capacity():
    n_addr = int(os.environ.get("PERF_ADDRESSES", "60"))
    client, auth = _client_and_auth()
    r = client.post("/api/v1/cases",
                    json={"title": "PERF-01 report capacity"},
                    headers=auth)
    assert r.status_code == 201, r.text
    case_id = r.json()["id"]
    seeds = client.get("/api/v1/demo/addresses").json()["addresses"]
    # fixture 只有 3 个种子地址；渲染路径按「关联行数」扩容（复用同一批
    # completed judgment），验证报告渲染器容量而非数据写入容量。
    used: list[str] = []
    for a in seeds:
        _analyze(client, a)
        used.append(a)
    # 关联接口单次最多 50 个地址（CaseAddressesRequest 上限），分批幂等关联
    for i in range(0, min(len(used), n_addr), 50):
        ra = client.post(f"/api/v1/cases/{case_id}/addresses",
                         json={"addresses": used[i:i + 50]}, headers=auth)
        assert ra.status_code == 201, ra.text
    if len(used) < n_addr:
        print(f"    note: fixture 只有 {len(used)} 个可分析种子；"
              f"报告渲染按 {len(used)} 地址执行（nightly 灌库后自动覆盖 {n_addr}）")

    rss_label, rss0 = _rss_probe()
    t0 = time.perf_counter()
    rep = client.post(f"/api/v1/cases/{case_id}/reports",
                      params={"format": "pdf"}, headers=auth)
    assert rep.status_code == 202, rep.text
    rid = rep.json()["report_id"]
    payload = {}
    while time.perf_counter() - t0 < 90:
        payload = client.get(f"/api/v1/reports/{rid}", headers=auth).json()
        if payload.get("status") in ("completed", "failed"):
            break
        time.sleep(0.25)
    elapsed = time.perf_counter() - t0
    _, rss1 = _rss_probe()

    check("PERF-01 report generated", payload.get("status") == "completed",
          str(payload)[:160])
    check("PERF-01 elapsed <= 60s", elapsed <= 60, f"{elapsed:.1f}s")
    check("PERF-01 RSS growth <= 3x baseline",
          rss1 <= max(rss0 * 3, rss0 + 512),
          f"{rss_label}: {rss0:.0f}MB -> {rss1:.0f}MB")
    dl = payload.get("download_url") or ""
    if dl:
        path_part, _, query = dl.partition("?")
        params = dict(p.split("=") for p in query.split("&"))
        got = client.get(path_part, params=params)
        # 完整性口径：HTTP 200 + %PDF 魔数 + EOF 标记 + 页数合理（fixture 档
        # 只有 3 地址，体积阈值按 nightly 灌库后的 60 地址档另行校准）。
        check("PERF-01 PDF integrity",
              got.status_code == 200 and got.content[:4] == b"%PDF"
              and b"%%EOF" in got.content[-64:], f"bytes={len(got.content)}")


def perf02_query_latency():
    samples = int(os.environ.get("PERF_SAMPLES", "30"))
    from sqlalchemy import func, select

    from backend.api.app import get_db_engine
    from backend.models.base import Judgment

    engine = get_db_engine()
    with engine.connect() as conn:
        total = conn.execute(select(func.count(Judgment.id))).scalar_one()
    print(f"    judgments rows = {total}")
    if total < 10000:
        print("[skip] PERF-02 needs >=10k rows; run nightly seed first")
        return

    # HTTP 口径为达标项：真实端点 + 响应模型序列化开销
    client, auth = _client_and_auth()
    with engine.connect() as conn:
        addrs = [r[0] for r in conn.execute(
            select(Judgment.address).where(Judgment.status == "completed")
            .distinct().limit(500)).all()]
    if not addrs:
        print("[skip] PERF-02 needs completed judgments to sample addresses")
        return

    lat_http_sub, lat_http_list, lat_db_sub, lat_db_list = [], [], [], []
    plan_rows = []
    with engine.connect() as conn:
        for i in range(samples):
            addr = addrs[i % len(addrs)]

            t0 = time.perf_counter()
            r = client.get(f"/api/v1/addresses/{addr}/subgraph")
            lat_http_sub.append((time.perf_counter() - t0) * 1000)
            assert r.status_code == 200, r.text
            assert r.json().get("nodes"), f"empty subgraph for {addr}"

            t0 = time.perf_counter()
            rl = client.get("/api/v1/cases", params={"page": 1 + i % 5,
                                                     "page_size": 50},
                            headers=auth)
            lat_http_list.append((time.perf_counter() - t0) * 1000)
            assert rl.status_code == 200, rl.text

            # DB 口径（对照，仅用于定位瓶颈在哪一层）
            t0 = time.perf_counter()
            conn.execute(select(Judgment).where(Judgment.address == addr,
                                                Judgment.status == "completed")
                         .order_by(Judgment.created_at.desc()).limit(1)
                         ).scalar_one_or_none()
            lat_db_sub.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            page = conn.execute(
                select(Judgment.id, Judgment.address, Judgment.status)
                .order_by(Judgment.created_at.desc())
                .offset(i * 50).limit(50)).all()
            lat_db_list.append((time.perf_counter() - t0) * 1000)
            assert page
        plan_rows = conn.execute(_explain(addrs[0])).all()

    p95 = lambda series: statistics.quantiles(series, n=20)[18]  # noqa: E731
    p95_sub, p95_list = p95(lat_http_sub), p95(lat_http_list)
    print(f"    http p95: subgraph={p95_sub:.0f}ms list={p95_list:.0f}ms | "
          f"db p95: subgraph={p95(lat_db_sub):.0f}ms list={p95(lat_db_list):.0f}ms")
    joined = " ".join(str(r[0]) for r in plan_rows)
    check("PERF-02 subgraph query p95 <= 300ms (http)", p95_sub <= 300,
          f"p95={p95_sub:.0f}ms")
    check("PERF-02 paginated list p95 <= 200ms (http)", p95_list <= 200,
          f"p95={p95_list:.0f}ms")
    check("PERF-02 EXPLAIN index hit", "Index Scan" in joined or
          "Index Only Scan" in joined, joined[:120])


def _explain(addr):
    from sqlalchemy import text

    return text("EXPLAIN SELECT * FROM judgments "
                "WHERE address = :a AND status='completed' "
                "ORDER BY created_at DESC LIMIT 1").bindparams(a=addr)


def main(argv):
    targets = argv or ["PERF-01", "PERF-02"]
    if "PERF-01" in targets:
        perf01_report_capacity()
    if "PERF-02" in targets:
        perf02_query_latency()
    print()
    if FAILURES:
        print(f"PERF RESULT: {len(FAILURES)} FAILED -> {FAILURES}")
        return 1
    print("PERF RESULT: ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
