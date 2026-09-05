"""阶段5 端到端验证 — 排期完成标志「登录 → 建案 → 关联地址 → 分析 → 导出报告」。

mock LLM provider + fixture 数据源，进程内驱动完整业务闭环：
    登录(investigator) → 建案(Idempotency-Key 幂等) → 关联地址 →
    触发分析并轮询至 completed → 导出 PDF/HTML 报告（异步轮询）→
    签名 URL 下载并校验证据链字段 → 审计日志串联（CM-10）→ 种子案例核对。

退出码 0 = 全部通过。

用法：LLM_PROVIDER=mock python tests/evaluation/run_e2e_phase5.py
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
    os.environ["LLM_PROVIDER"] = "mock"
    os.environ["GRAPH_DATA_MODE"] = "fixture"
    os.environ.pop("LLM_MOCK_SCENARIO", None)
    from backend.core.config import reset_settings

    reset_settings()

    from fastapi.testclient import TestClient

    from backend.api.app import create_app, seed_user

    app = create_app()
    client = TestClient(app)
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})  # SEC-03

    seeds = client.get("/api/v1/demo/addresses").json()["addresses"]
    check("three fixture seeds available", len(seeds) >= 3,
          f"seeds={len(seeds)}")

    # ---- 1. 登录 ----
    seed_user("inv@demo.local", "InvestDemo!2026", role="investigator")
    r = client.post("/api/v1/auth/login",
                    json={"email": "inv@demo.local",
                          "password": "InvestDemo!2026"})
    check("login returns access_token", r.status_code == 200)
    check("Set-Cookie refresh_token HttpOnly (SEC-02)",
          "httponly" in r.headers.get("set-cookie", "").lower())
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # ---- 2. 建案（幂等键）----
    key = {"Idempotency-Key": "e2e-phase5-case"}
    r1 = client.post("/api/v1/cases", json={"title": "E2E 闭环演示案件"},
                     headers={**auth, **key})
    r2 = client.post("/api/v1/cases", json={"title": "E2E 闭环演示案件"},
                     headers={**auth, **key})
    check("create case 201", r1.status_code == 201, r1.text[:200])
    check("idempotent replay same case_id (BE-23)",
          r2.status_code == 200 and r2.json()["id"] == r1.json()["id"])
    case_id = r1.json()["id"]

    # ---- 3. 关联地址（重复关联幂等跳过）----
    demo_addr = seeds[0]
    a1 = client.post(f"/api/v1/cases/{case_id}/addresses",
                     json={"addresses": [demo_addr]}, headers=auth)
    a2 = client.post(f"/api/v1/cases/{case_id}/addresses",
                     json={"addresses": [demo_addr]}, headers=auth)
    check("associate address 201", a1.status_code == 201, a1.text[:200])
    check("duplicate association idempotent (BE-24)", a2.status_code == 200)

    # ---- 4. 分析该地址并轮询至 completed ----
    jid = _analyze(client, demo_addr)

    # 案件详情应带出最新 judgment 摘要
    detail = client.get(f"/api/v1/cases/{case_id}", headers=auth).json()
    linked_judgment = next(
        (a for a in detail["addresses"]
         if a["address"] == demo_addr), {}).get("latest_judgment")
    check("case detail exposes latest judgment summary",
          bool(linked_judgment and linked_judgment.get("risk_level")))

    # ---- 5. 导出报告（异步 + 轮询）----
    rep = client.post(f"/api/v1/cases/{case_id}/reports",
                      params={"format": "pdf"}, headers=auth)
    check("export report accepted 202", rep.status_code == 202, rep.text[:200])
    rid = rep.json()["report_id"]

    report = {}
    deadline = time.time() + 20
    while time.time() < deadline:
        report = client.get(f"/api/v1/reports/{rid}", headers=auth).json()
        if report["status"] in ("completed", "failed"):
            break
        time.sleep(0.3)
    check("report completed", report.get("status") == "completed",
          str(report))
    dl_url = report.get("download_url") or ""
    check("download url signed with exp+sig",
          "exp=" in dl_url and "sig=" in dl_url)

    # ---- 6. 下载并校验（PDF：格式与体积；证据链字段在 HTML 报告中验）----
    path_part, _, query = dl_url.partition("?")
    params = dict(p.split("=") for p in query.split("&"))
    got = client.get(path_part, params=params)
    check("download 200 with %PDF magic",
          got.status_code == 200 and got.content[:4] == b"%PDF"
          and len(got.content) > 1000,
          f"status={got.status_code} head={got.content[:8]!r}")
    judgment = client.get(f"/api/v1/judgments/{jid}").json()

    # 过期签名拒绝（BE-50）
    tampered = dict(params, exp=str(int(time.time()) - 100))
    expired = client.get(path_part, params=tampered)
    check("expired signature rejected 403 (BE-50)",
          expired.status_code == 403)

    # HTML 报告同样可用且含完整 reasoning
    rep_h = client.post(f"/api/v1/cases/{case_id}/reports",
                        params={"format": "html"}, headers=auth)
    rid_h = rep_h.json()["report_id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        report_h = client.get(f"/api/v1/reports/{rid_h}",
                              headers=auth).json()
        if report_h["status"] in ("completed", "failed"):
            break
        time.sleep(0.3)
    got_h = client.get(report_h["download_url"].partition("?")[0],
                       params=dict(p.split("=") for p in
                                   report_h["download_url"].partition("?")[2].split("&")))
    chain_markers = [judgment.get("model") or "",
                     judgment.get("builder_version") or "",
                     (judgment.get("prompt_version") or "")]
    check("html report renders full reasoning",
          got_h.status_code == 200
          and (judgment.get("reasoning") or "")[:80] in got_h.text)
    check("html report embeds evidence chain (model/prompt/builder)",
          all(m in got_h.text for m in chain_markers if m),
          f"markers={chain_markers}")

    # ---- 7. 审计链路（CM-10）：login→create_case→associate→export 均留痕 ----
    admin_ok = _seed_admin_and_verify_audit(client, {
        "login", "create_case", "associate_address", "export_report"})
    check("audit trail covers full workflow (CM-10)", admin_ok)

    print()
    if FAILURES:
        print(f"E2E RESULT: {len(FAILURES)} FAILED -> {FAILURES}")
        return 1
    print("E2E RESULT: ALL PASSED")
    return 0


def _analyze(client, address: str) -> str:
    r = client.post("/api/v1/addresses/analyze",
                    json={"address": address})
    assert r.status_code == 202, r.text
    jid = r.json()["judgment_id"]
    deadline = time.time() + 30
    payload = {}
    while time.time() < deadline:
        payload = client.get(f"/api/v1/judgments/{jid}").json()
        if payload.get("status") in ("completed", "failed"):
            break
        time.sleep(0.25)
    assert payload["status"] == "completed", payload
    return jid


def _seed_admin_and_verify_audit(client, expected_actions: set[str]) -> bool:

    from backend.api.app import seed_user

    seed_user("admin@demo.local", "AdminDemo!2026", role="admin")
    r = client.post("/api/v1/auth/login",
                    json={"email": "admin@demo.local",
                          "password": "AdminDemo!2026"})
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    listed = client.get("/api/v1/audit-logs",
                        params={"page_size": 50}, headers=auth).json()
    actions = {i["action"] for i in listed["items"]}
    if not expected_actions.issubset(actions):
        print(f"    audit actions found: {sorted(actions)}")
        return False
    # request_id 字段完整性抽检
    item = listed["items"][0]
    return all(item.get(k) is not None
               for k in ("request_id", "http_method", "http_path"))


if __name__ == "__main__":
    raise SystemExit(main())
