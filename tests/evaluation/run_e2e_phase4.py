"""阶段4 端到端验证 — 排期完成标志「地址 → 子图 → 判断 → 可视化」跑通。

mock LLM provider + fixture 图数据源，进程内驱动完整管线（不依赖公网/
本地 Ollama）：
    匿名白名单 analyze(202) → 轮询至 completed → D3/evidence 引用闭合校验
    → subgraph 快照查询 → 幂等语义 → invalid-evidence 失败路径 → patterns 分页。

退出码 0 = 全部通过；每步打印 [ok]/[FAIL] 供 verify_phase4.sh 收集。

用法：python tests/evaluation/run_e2e_phase4.py
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

    # ---- 匿名白名单分析（BE-08）----
    seeds = client.get("/api/v1/demo/addresses").json()["addresses"]
    check("demo addresses exposed", len(seeds) >= 1)
    demo_addr = seeds[0]

    _clear_judgments()
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
              risk in {"high", "medium", "low", "no_match"}, str(risk))
        check("confidence in [0,1]",
              0.0 <= (payload.get("confidence") or 0.0) <= 1.0,
              str(payload.get("confidence")))
        snap = payload["subgraph"]
        snap_ids = ({n["id"] for n in snap["nodes"]}
                    | {e["id"] for e in snap["edges"]})
        check("subgraph rendered nodes", len(snap["nodes"]) > 0,
              f"nodes={len(snap['nodes'])}")
        check("node ids follow D3",
              all(n["id"].startswith(("addr:", "tx:")) for n in snap["nodes"]))
        ev = payload.get("evidence") or []
        check("evidence non-empty", len(ev) > 0)
        check("evidence ⊆ snapshot ids (引用闭合)",
              all(e in snap_ids for e in ev),
              f"ev={ev} missing={[e for e in ev if e not in snap_ids]}")
        check("model recorded", payload.get("model") == "mock-demo-model")
        check("prompt/builder version recorded",
              bool(payload.get("prompt_version"))
              and bool(payload.get("builder_version")))
        check("latency_ms recorded",
              isinstance(payload.get("latency_ms"), int))

        # ---- 可视化数据链路：前端消费的同一 subgraph 端点（BE-33）----
        r = client.get(f"/api/v1/addresses/{demo_addr}/subgraph", headers=auth)
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
    os.environ["LLM_MOCK_SCENARIO"] = "invalid_evidence_all_retries"
    r = client.post("/api/v1/addresses/analyze",
                    json={"address": demo_addr, "hops": 2})
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
    os.environ.pop("LLM_MOCK_SCENARIO", None)

    # ---- patterns 分页契约（BE-21/22 结构性断言；KB 规模由门禁保证）----
    r = client.get("/api/v1/patterns",
                   params={"page_size": 500}, headers=auth)
    check("patterns page_size>100 rejected", r.status_code == 422)
    r = client.get("/api/v1/patterns",
                   params={"page_size": 5, "evidence_grade": "A"},
                   headers=auth)
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


def _poll(client, jid: str, auth: dict, timeout_s: float = 30.0) -> dict:
    deadline = time.time() + timeout_s
    payload: dict = {}
    while time.time() < deadline:
        payload = client.get(f"/api/v1/judgments/{jid}", headers=auth).json()
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
