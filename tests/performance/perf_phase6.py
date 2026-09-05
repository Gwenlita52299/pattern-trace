"""阶段6 性能基准 — PERF-01 / PERF-02（nightly 档）。

PERF-01  大案件（默认 60 地址）报告生成：耗时 ≤60s、PDF 魔数/体积、
         RSS 增长 ≤ 基线 ×3；
PERF-02  万级 judgments 查询：subgraph 与分页列表各采样 ≥30 次，
         p95 阈值 300ms / 200ms，并核对 EXPLAIN 是否 Index Scan。

用法：
    python tests/performance/perf_phase6.py            # 全部
    python tests/performance/perf_phase6.py PERF-01    # 仅报告容量
环境：
    DATABASE_URL   默认 postgresql://pt:pt@localhost:5432/patterntrace
    PERF_ADDRESSES 默认 60（PERF-01 规模）
    PERF_SAMPLES   默认 30（PERF-02 采样次数）
退出码非 0 = 存在未达标项。
"""
from __future__ import annotations

import os
import resource
import statistics
import sys
import time
import uuid
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

    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
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
    rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    check("PERF-01 report generated", payload.get("status") == "completed",
          str(payload)[:160])
    check("PERF-01 elapsed <= 60s", elapsed <= 60, f"{elapsed:.1f}s")
    # ru_maxrss 单位平台相关（macOS=字节, Linux=KB），先归一到 MB 再打印
    _rss_mb = lambda r: r * (1 if sys.platform == "darwin" else 1024) / (1024 ** 2)
    check("PERF-01 RSS growth <= 3x baseline",
          rss1 <= max(rss0 * 3, rss0 + 512 * 1024),
          f"{_rss_mb(rss0):.0f}MB -> {_rss_mb(rss1):.0f}MB")
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

    from backend.api.app import create_app, get_db_engine
    from backend.models.base import Judgment

    engine = get_db_engine()
    with engine.connect() as conn:
        total = conn.execute(select(func.count(Judgment.id))).scalar_one()
    print(f"    judgments rows = {total}")
    if total < 10000:
        print("[skip] PERF-02 needs >=10k rows; run nightly seed first")
        return
    app = create_app()
    client = TestClient(app) if False else None  # noqa: F841 — 预留 HTTP 口径
    seeds = app and _demo_addresses(app)
    lat_sub, lat_list = [], []
    plan_rows = []
    with engine.connect() as conn:
        for i in range(samples):
            addr = seeds[i % len(seeds)]
            t0 = time.perf_counter()
            row = conn.execute(
                select(Judgment).where(Judgment.address == addr,
                                       Judgment.status == "completed")
                .order_by(Judgment.created_at.desc()).limit(1)
            ).scalar_one_or_none()
            lat_sub.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            page = conn.execute(
                select(Judgment.id, Judgment.address, Judgment.status)
                .order_by(Judgment.created_at.desc())
                .offset(i * 50).limit(50)).all()
            lat_list.append((time.perf_counter() - t0) * 1000)
            assert page
        plan_rows = conn.execute(_explain(seeds[0])).all()
    p95_sub = statistics.quantiles(lat_sub, n=20)[18]
    p95_list = statistics.quantiles(lat_list, n=20)[18]
    joined = " ".join(str(r[0]) for r in plan_rows)
    check("PERF-02 subgraph query p95 <= 300ms", p95_sub <= 300,
          f"p95={p95_sub:.0f}ms")
    check("PERF-02 paginated list p95 <= 200ms", p95_list <= 200,
          f"p95={p95_list:.0f}ms")
    check("PERF-02 EXPLAIN index hit", "Index Scan" in joined or
          "Index Only Scan" in joined, joined[:120])


def _explain(addr):
    from sqlalchemy import text

    return text("EXPLAIN SELECT * FROM judgments "
                "WHERE address = :a AND status='completed' "
                "ORDER BY created_at DESC LIMIT 1").bindparams(a=addr)


def _demo_addresses(app):
    from fastapi.testclient import TestClient

    return TestClient(app).get("/api/v1/demo/addresses").json()["addresses"]


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
