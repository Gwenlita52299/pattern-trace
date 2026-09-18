"""issue #78 — E2E 统一入口：完整分析→判断→子图→报告业务闭环。

两种模式：
- 缺省（TestClient）：进程内驱动，供本地门禁/verify_phase6.sh；
- --base-url URL（compose HTTP 模式）：走真实 HTTP（CI 中经前端
  同源代理 http://localhost:13000/api/v1），不使用 TestClient 私有
  初始化、不进程内执行 run_analysis、不依赖同进程 os.environ 注入
  worker 故障（mock 场景经请求字段 mock_scenario 注入，issue #78）。

worker-gap 模式（--worker-gap CMD_PREFIX）：证明实际消费来自独立
worker——停 worker 后提交任务，有界等待内保持 queued，启动后完成。
禁止悄悄回落进程内（Redis 可达时 dispatch 只入队不执行，此断言
即验证该形态）。

退出码非零 = 有断言失败；各步打印 [ok]/[FAIL]。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

INVESTIGATOR_EMAIL = "inv@demo.local"
INVESTIGATOR_PASSWORD = "InvestDemo!2026"
ADMIN_EMAIL = "admin@demo.local"
ADMIN_PASSWORD = "AdminDemo!2026"


def _prepare_http(base_url: str):
    """HTTP 模式用户准备：bootstrap admin 登录 → 建号幂等（409 复用）。"""
    import httpx

    # base_url 规范化到 host：httpx 的 base_url 拼接相对路径
    # （/api/v1/...）会出现 /api/v1/api/v1/... 双前缀
    base_url = base_url.rstrip("/")
    if base_url.endswith("/api/v1"):
        base_url = base_url[: -len("/api/v1")]
    client = httpx.Client(
        base_url=base_url, timeout=30.0,
        headers={"X-Requested-With": "XMLHttpRequest"})  # SEC-03

    bootstrap_email = os.environ["BOOTSTRAP_ADMIN_EMAIL"]
    bootstrap_password = os.environ["BOOTSTRAP_ADMIN_PASSWORD"]
    r = client.post("/api/v1/auth/login",
                    json={"email": bootstrap_email,
                          "password": bootstrap_password})
    assert r.status_code == 200, f"bootstrap admin login failed: {r.text}"
    admin_auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    # 建号幂等：已存在则 409，直接复用
    for email, password, role in (
            (INVESTIGATOR_EMAIL, INVESTIGATOR_PASSWORD, "investigator"),
            (ADMIN_EMAIL, ADMIN_PASSWORD, "admin")):
        rc = client.post("/api/v1/users",
                         json={"email": email, "password": password,
                               "role": role}, headers=admin_auth)
        assert rc.status_code in (201, 409), f"create user {email}: {rc.text}"
    os.environ["E2E_HTTP"] = "1"
    return client


def _prepare_inprocess():
    os.environ["LLM_PROVIDER"] = "mock"
    os.environ["LLM_MODEL"] = "mock-demo-model"
    os.environ["GRAPH_DATA_MODE"] = "fixture"
    os.environ["E2E_INPROCESS"] = "1"
    os.environ.pop("LLM_MOCK_SCENARIO", None)
    os.environ.setdefault(
        "DATABASE_URL", "postgresql://pt:pt@localhost:5432/patterntrace")
    # 进程内模式没有 worker 消费队列：把 Redis 指向不可达端口，
    # dispatch_analysis 走显式的进程内降级路径（任务在测试进程内完成）。
    # 附带效果：判决缓存同样降级 InMemoryCache——不触开发 Redis。
    os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"
    from backend.core.config import reset_settings

    reset_settings()

    from fastapi.testclient import TestClient

    from backend.api.app import create_app, seed_user

    seed_user(INVESTIGATOR_EMAIL, INVESTIGATOR_PASSWORD, role="investigator")
    seed_user(ADMIN_EMAIL, ADMIN_PASSWORD, role="admin")
    # phase4/phase5 的固定账号（历史入口兼容）
    seed_user("inv@demo.local", "InvestDemo!2026", role="investigator")
    seed_user("admin@demo.local", "AdminDemo!2026", role="admin")

    app = create_app()
    client = TestClient(app)
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    return client


def worker_gap(cmd_prefix: str, artifact_dir: Path | None) -> int:
    """停 worker → 任务保持 queued → 启动 worker → 完成（验收第 5 条）。"""
    import httpx

    cmd = cmd_prefix.split()
    base_url = os.environ.get("E2E_BASE_URL", "http://localhost:18000")
    base_url = base_url.rstrip("/")
    if base_url.endswith("/api/v1"):
        base_url = base_url[: -len("/api/v1")]
    client = httpx.Client(base_url=base_url, timeout=30.0,
                          headers={"X-Requested-With": "XMLHttpRequest"})
    r = client.post("/api/v1/auth/login",
                    json={"email": INVESTIGATOR_EMAIL,
                          "password": INVESTIGATOR_PASSWORD})
    assert r.status_code == 200, r.text
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

    seeds = client.get("/api/v1/demo/addresses").json()["addresses"]
    addr = seeds[0]

    subprocess.run([*cmd, "stop", "worker"], check=True)
    try:
        r = client.post("/api/v1/addresses/analyze",
                        json={"address": addr, "hops": 2})
        assert r.status_code in (202, 200), r.text
        jid = r.json()["judgment_id"]
        # 有界等待：无 worker 时必须保持 queued（未被 API 进程执行）
        payload = {}
        deadline = time.time() + 20
        while time.time() < deadline:
            payload = client.get(f"/api/v1/judgments/{jid}",
                                 headers=auth).json()
            if payload.get("status") != "queued":
                break
            time.sleep(0.5)
        ok = payload.get("status") == "queued"
        print(f"[{'ok' if ok else 'FAIL'}] worker stopped: task stays queued"
              + ("" if ok else f" — {payload}"))
        if not ok:
            return 1
    finally:
        subprocess.run([*cmd, "start", "worker"], check=True)
        # worker 启动钩子 recover_stuck_tasks 会重新 enqueue，若消息丢失
        # 由 arq 队列直接消费——两种形态最终都应完成

    deadline = time.time() + 90
    payload = {}
    while time.time() < deadline:
        payload = client.get(f"/api/v1/judgments/{jid}", headers=auth).json()
        if payload.get("status") in ("completed", "failed"):
            break
        time.sleep(1.0)
    ok = payload.get("status") == "completed"
    print(f"[{'ok' if ok else 'FAIL'}] worker resumed: analysis completed"
          + ("" if ok else f" — {payload}"))
    if artifact_dir:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "worker_gap_summary.json").write_text(json.dumps(
            {"judgment_id": jid, "final": payload}, ensure_ascii=False))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=None,
                    help="compose HTTP 模式的 API 根 URL（缺省 TestClient）")
    ap.add_argument("--worker-gap", default=None, metavar="COMPOSE_CMD",
                    help="仅跑停 worker 用例；COMPOSE_CMD 为 compose 命令前缀")
    ap.add_argument("--artifact-dir", default="/tmp/pt-e2e",
                    help="摘要产物目录（CI always 上传）")
    args = ap.parse_args()

    artifact_dir = Path(args.artifact_dir)
    os.environ["E2E_ARTIFACT_DIR"] = str(artifact_dir)

    if args.worker_gap:
        os.environ["E2E_BASE_URL"] = args.base_url \
            or "http://localhost:18000/api/v1"
        return worker_gap(args.worker_gap, artifact_dir)

    if args.base_url:
        client = _prepare_http(args.base_url)
        os.environ["E2E_BASE_URL"] = args.base_url
    else:
        client = _prepare_inprocess()

    from tests.evaluation import run_e2e_phase4, run_e2e_phase5

    rc1 = run_e2e_phase4.run_pipeline(client, auth=None)
    rc2 = run_e2e_phase5.run_pipeline(client)
    rc = int(rc1 != 0) + int(rc2 != 0)
    if rc:
        _dump_artifacts(client, artifact_dir)
    return 1 if rc else 0


def _dump_artifacts(client, artifact_dir: Path) -> None:
    """失败产物：judgment/event/检索摘要（去敏感：仅测试环境数据）。"""
    try:
        r = client.get("/api/v1/patterns", params={"page_size": 100})
        (artifact_dir / "patterns.json").write_text(r.text)
        r = client.get("/api/v1/demo/addresses")
        (artifact_dir / "demo_addresses.json").write_text(r.text)
    except Exception as exc:  # noqa: BLE001 — 产物尽力而为
        print(f"[warn] artifact dump partial: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
