"""阶段4 端到端验证 — 排期完成标志「地址 → 子图 → 判断 → 可视化」跑通。

mock LLM provider + fixture 图数据源，驱动完整管线（不依赖公网/本地 Ollama）：
    匿名白名单 analyze(202) → 轮询至 completed → D3/evidence 引用闭合校验
    → subgraph 快照查询 → 幂等语义 → invalid-evidence 失败路径 → patterns 分页
    → 检索有效性断言（issue #78：KB 非空、候选快照、matched ∈ 本次候选、
      evidence ⊆ 快照 addr 节点、阶段顺序持久化序列、no_match 用例）。

退出码 0 = 全部通过；每步打印 [ok]/[FAIL] 供门禁脚本收集。

用法：python tests/evaluation/run_e2e_phase4.py        # TestClient 进程内模式
      由 tests/evaluation/run_e2e.py 注入 httpx client  # compose HTTP 模式
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "ok" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    # --- 环境隔离：settings 缓存必须在设好环境变量后重建 ---
    os.environ["LLM_PROVIDER"] = "mock"
    os.environ["LLM_MODEL"] = "mock-demo-model"
    os.environ["GRAPH_DATA_MODE"] = "fixture"
    os.environ.pop("LLM_MOCK_SCENARIO", None)
    from backend.core.config import reset_settings

    reset_settings()

    from fastapi.testclient import TestClient

    from backend.api.app import create_app

    app = create_app()
    client = TestClient(app)
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})  # SEC-03

    # ---- 登录（受保护查询接口需要）----
    import backend.api.app as app_module

    app_module.seed_user("admin@test.com", "AdminP@ss1", role="admin")
    r = client.post("/api/v1/auth/login",
                    json={"email": "admin@test.com", "password": "AdminP@ss1"})
    check("login ok", r.status_code == 200)
    token = r.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    _clear_judgments()
    return run_pipeline(client, auth)


def run_pipeline(client, auth: dict | None) -> int:
    """E2E 主体；client 为 TestClient（进程内）或 httpx.Client（compose）。

    mock 场景经请求字段 mock_scenario 注入（issue #78）——两种模式
    同一路径，不依赖同进程 os.environ 修改。
    """
    # ---- 匿名白名单分析（BE-08）----
    seeds = client.get("/api/v1/demo/addresses").json()["addresses"]
    check("demo addresses exposed", len(seeds) >= 1)
    demo_addr = seeds[0]

    # ---- KB 就绪（issue #78 验收第 3 条：fixture KB seeded 且非空）----
    r = client.get("/api/v1/patterns", params={"page_size": 100},
                   headers=auth or {})
    items = r.json().get("items", [])
    check("patterns endpoint reachable", r.status_code == 200,
          f"status={r.status_code}")
    check("KB non-empty (fixture seeded)", len(items) > 0,
          f"items={len(items)}")

    t0 = time.perf_counter()
    r = client.post("/api/v1/addresses/analyze",
                    json={"address": demo_addr, "hops": 3})
    latency_submit = (time.perf_counter() - t0) * 1000
    check("anon analyze returns 202", r.status_code == 202,
          f"got {r.status_code}: {r.text[:200]}")
    jid = r.json()["judgment_id"]
    check("poll_url contract",
          r.json()["poll_url"] == f"/api/v1/judgments/{jid}")
    print(f"    submit overhead: {latency_submit:.1f}ms  judgment={jid}")

    # ---- 轮询至终态并校验产物（BE-13 + 验收标准第 6 条）----
    payload = _poll(client, jid, auth)
    check("reached completed", payload.get("status") == "completed",
          str({k: payload.get(k) for k in ("status", "error_code")}))
    if payload.get("status") == "completed":
        risk = payload.get("risk_level")
        check("risk_level in enum",
              risk in {"high", "medium", "low"}, str(risk))
        check("confidence in [0,1]",
              0.0 <= (payload.get("confidence") or 0.0) <= 1.0,
              str(payload.get("confidence")))
        snap = payload["subgraph"]
        snap_ids = ({n["id"] for n in snap["nodes"]}
                    | {e["id"] for e in snap["edges"]})
        addr_ids = {n["id"] for n in snap["nodes"]
                    if n.get("kind") == "address"}
        check("subgraph rendered nodes", len(snap["nodes"]) > 0,
              f"nodes={len(snap['nodes'])}")
        check("node ids follow D3",
              all(n["id"].startswith(("addr:", "tx:")) for n in snap["nodes"]))
        ev = payload.get("evidence") or []
        check("evidence non-empty", len(ev) > 0)
        check("evidence ⊆ snapshot ids (引用闭合)",
              all(e in snap_ids for e in ev),
              f"ev={ev} missing={[e for e in ev if e not in snap_ids]}")
        # issue #78：evidence 必须是快照中的 addr 节点（mock 物证统一 addr）
        check("evidence ⊆ snapshot addr nodes",
              all(e in addr_ids for e in ev),
              f"ev={ev} addr_ids={sorted(addr_ids)[:5]}")
        check("model recorded", payload.get("model") == "mock-demo-model")
        check("prompt/builder version recorded",
              bool(payload.get("prompt_version"))
              and bool(payload.get("builder_version")))
        check("latency_ms recorded",
              isinstance(payload.get("latency_ms"), int))

        # ---- 检索有效性（issue #78 验收第 3 条，正例）----
        events = _read_events(jid)
        retrieval_rows = [e for e in events if e["to"] == "stage:retrieval_done"]
        check("retrieval snapshot persisted", bool(retrieval_rows))
        cands = (retrieval_rows[-1]["detail"] or {}).get("candidates", []) \
            if retrieval_rows else []
        cand_names = [c.get("name") for c in cands]
        check("retrieval returned non-zero candidates", len(cands) > 0,
              f"candidates={cand_names}")
        matched = payload.get("matched_pattern_name")
        check("matched_pattern ∈ 本次候选集合",
              matched in cand_names,
              f"matched={matched} candidates={cand_names}")
        # mock valid_high 提取 prompt 中首个候选名（= 精排 Top-1），确定预期
        check("matched_pattern == rerank Top-1 (确定性正例)",
              bool(cand_names) and matched == cand_names[0],
              f"matched={matched} top1={cand_names[:1]}")
        _assert_stage_sequence(events, jid)

        # ---- 检索解释快照端点（issue #77）----
        exp = client.get(
            f"/api/v1/judgments/{jid}/retrieval-explanation",
            headers=auth or {})
        check("retrieval-explanation 200", exp.status_code == 200,
              str(exp.status_code))
        if exp.status_code == 200:
            snap = exp.json()
            check("explanation params 可复现",
                  snap.get("algorithm_version") == "retr-v1"
                  and (snap.get("params") or {}).get("top_k", 0) >= 1,
                  str(snap.get("params")))
            check("explanation recall 元信息",
                  (snap.get("recall") or {}).get("mode") in
                  ("graphormer_online", "graphormer_ego", "hybrid"),
                  str(snap.get("recall")))
            cands_e = snap.get("candidates") or []
            check("explanation 候选含 rank+全分数",
                  bool(cands_e) and all(
                      c.get("rank") == i
                      and "wl_kernel_score" in c
                      and "similarity_score" in c
                      and "provenance" in c
                      for i, c in enumerate(cands_e, start=1)),
                  f"n={len(cands_e)}")
            check("explanation 候选与 judge 输入同源",
                  matched in [c.get("name") for c in cands_e]
                  if matched else True,
                  f"matched={matched}")

        # ---- 可视化数据链路：前端消费的同一 subgraph 端点（BE-33）----
        r = client.get(f"/api/v1/addresses/{demo_addr}/subgraph",
                       headers=auth or {})
        check("GET subgraph returns latest snapshot",
              r.status_code == 200 and len(r.json()["nodes"]) > 0)

    # ---- 幂等语义（BE-12 反例）：completed 后同参数提交产生新任务 ----
    r = client.post("/api/v1/addresses/analyze",
                    json={"address": demo_addr, "hops": 3})
    body = r.json()
    check("resubmit after completed creates NEW judgment",
          r.status_code == 202 and body["judgment_id"] != jid,
          f"{r.status_code} {body}")
    _poll(client, body["judgment_id"], auth)  # 等它结束避免残留 active 行

    # ---- 失败路径（BE-14）：三次重试全为非法引用 → failed 终态 ----
    r = client.post("/api/v1/addresses/analyze",
                    json={"address": demo_addr, "hops": 2,
                          "mock_scenario": "invalid_evidence_all_retries"})
    fid = r.json()["judgment_id"]
    fpayload = _poll(client, fid, auth)
    check("failed terminal reached", fpayload.get("status") == "failed",
          str(fpayload.get("status")))
    check("error_code = LLM_VALIDATION_FAILED",
          fpayload.get("error_code") == "LLM_VALIDATION_FAILED")
    check("retry_count = 2", fpayload.get("retry_count") == 2,
          str(fpayload.get("retry_count")))
    check("failed_at recorded", bool(fpayload.get("failed_at")))
    check("no verdict fields on failed",
          fpayload.get("risk_level") is None
          and fpayload.get("confidence") is None)

    # ---- flagged_no_pattern 用例（issue #72：mock 场景 matched=null + medium；
    # 枚举重构后确定性 no_match 语义并入此档，走完整管线）----
    r = client.post("/api/v1/addresses/analyze",
                    json={"address": demo_addr, "hops": 1,
                          "mock_scenario": "valid_no_match"})
    nid = r.json()["judgment_id"]
    npayload = _poll(client, nid, auth)
    check("flagged_no_pattern completed",
          npayload.get("status") == "completed",
          str({k: npayload.get(k) for k in ("status", "error_code")}))
    check("flagged_no_pattern risk medium",
          npayload.get("risk_level") == "medium",
          str(npayload.get("risk_level")))
    check("flagged_no_pattern matched_pattern null",
          npayload.get("matched_pattern_name") is None)
    check("flagged_no_pattern recommended_action review",
          npayload.get("recommended_action") == "review",
          str(npayload.get("recommended_action")))
    check("no_match evidence empty by contract",
          not (npayload.get("evidence") or []))
    nevents = _read_events(nid)
    nretr = [e for e in nevents if e["to"] == "stage:retrieval_done"]
    check("no_match still runs real retrieval",
          bool(nretr) and (nretr[-1]["detail"] or {}).get("candidates"),
          "empty retrieval events")

    # ---- patterns 分页契约（BE-21/22 结构性断言）----
    r = client.get("/api/v1/patterns",
                   params={"page_size": 500}, headers=auth or {})
    check("patterns page_size>100 rejected", r.status_code == 422)
    r = client.get("/api/v1/patterns",
                   params={"page_size": 5, "evidence_grade": "A"},
                   headers=auth or {})
    body = r.json()
    check("patterns envelope shape",
          all(k in body for k in ("items", "total", "page", "page_size", "pages")))
    check("grade filter respected",
          all(i["evidence_grade"] == "A" for i in body.get("items", [])))

    print()
    if FAILURES:
        print(f"E2E RESULT: {len(FAILURES)} FAILED -> {FAILURES}")
        return 1
    print("E2E RESULT: ALL PASSED")
    return 0


def _assert_stage_sequence(events: list[dict], jid: str) -> None:
    """issue #78：阶段顺序经持久化记录验证（HTTP 轮询会跳过快阶段）。"""
    order = ["stage:retrieval_topk", "stage:wl_rerank",
             "stage:retrieval_done", "stage:llm_judging"]
    seen = [e["to"] for e in events]
    idx = [next((i for i, s in enumerate(seen) if s == st), -1)
           for st in order]
    check("stage sequence persisted in order",
          all(i >= 0 for i in idx) and idx == sorted(idx),
          f"events={seen}")
    statuses = [e["to"] for e in events if not e["to"].startswith("stage:")]
    check("status transitions queued→processing→completed",
          statuses == ["processing", "completed"], f"got {statuses}")


def _read_events(jid: str) -> list[dict]:
    """judgment_events 观测点直读（测试观测通道，非生产 API）。

    HTTP 模式（compose）经 DATABASE_URL 直连；TestClient 模式同库。
    """
    from sqlalchemy import create_engine, text

    url = os.environ.get(
        "DATABASE_URL", "postgresql://pt:pt@localhost:5432/patterntrace")
    engine = create_engine(url.replace(
        "postgresql://", "postgresql+psycopg://"))
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT to_status, detail FROM judgment_events "
            "WHERE judgment_id = :j ORDER BY id"), {"j": jid}).mappings().all()
    return [{"to": r["to_status"], "detail": r["detail"]} for r in rows]


def _poll(client, jid: str, auth: dict | None,
          timeout_s: float = 30.0) -> dict:
    deadline = time.time() + timeout_s
    payload: dict = {}
    while time.time() < deadline:
        payload = client.get(f"/api/v1/judgments/{jid}",
                             headers=auth or {}).json()
        if payload.get("status") in ("completed", "failed"):
            return payload
        time.sleep(0.25)
    return payload


def _clear_judgments() -> None:
    from sqlalchemy import delete
    from sqlalchemy.orm import Session

    from backend.api.app import get_db_engine
    from backend.core.config import get_settings
    from backend.models.base import Judgment

    with Session(get_db_engine()) as session:
        session.execute(delete(Judgment))
        session.commit()
    # 判决缓存跨进程存活于 Redis——保证 e2e 每次走真实 LLM 路径
    try:
        import redis

        client = redis.Redis.from_url(get_settings().redis_url,
                                      socket_connect_timeout=1)
        keys = list(client.scan_iter(match="gb-v1:*", count=100))
        if keys:
            client.delete(*keys)
    except Exception:  # noqa: BLE001, S110 — 无 Redis 时缓存本就是进程内的
        pass


if __name__ == "__main__":
    raise SystemExit(main())
