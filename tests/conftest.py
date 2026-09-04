import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 环境隔离：JWT_SECRET 必须在 import config 前设置
os.environ.setdefault("JWT_SECRET", "test-secret-for-ci-only")

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app, reset_stores, seed_user

# SEC-03：写请求强制 X-Requested-With；测试客户端统一携带（与前端 api.ts 一致）
CSRF_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture(autouse=True)
def _force_inprocess_queue(monkeypatch):
    """issue #22：单测环境没有 worker 消费 Redis 队列。

    开发机若恰好在跑 Redis，enqueue 会成功但任务永远排队 → 分析测试挂起。
    统一强制走 task_queue 的进程内降级路径，保证测试确定性；真实队列路径
    在 test_issue22_persistent_queue.py 里用 fake pool 单独覆盖。
    """

    async def _enqueue_fails(*args, **kwargs):
        return False

    monkeypatch.setattr("backend.services.task_queue._enqueue",
                        _enqueue_fails)


@pytest.fixture(autouse=True)
def _reset():
    reset_stores()
    yield
    reset_stores()


@pytest.fixture()
def client():
    c = TestClient(create_app())
    c.headers.update(CSRF_HEADERS)
    return c


@pytest.fixture()
def investigator_client(client):
    seed_user("inv@test.com", "Passw0rd!123", role="investigator")
    resp = client.post("/api/v1/auth/login",
                       json={"email": "inv@test.com", "password": "Passw0rd!123"})
    token = resp.json()["access_token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture()
def admin_client(client):
    seed_user("admin@test.com", "AdminP@ss1", role="admin")
    resp = client.post("/api/v1/auth/login",
                       json={"email": "admin@test.com", "password": "AdminP@ss1"})
    token = resp.json()["access_token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client
