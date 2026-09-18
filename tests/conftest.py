import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 环境隔离：JWT_SECRET 必须在 import config 前设置。
# issue #27 后 Settings 拒绝占位/弱密钥，测试注入显式开发密钥（64 hex）
TEST_JWT_SECRET = "0123456789abcdef" * 4
os.environ.setdefault("JWT_SECRET", TEST_JWT_SECRET)

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
def _force_memory_rate_limiter(monkeypatch):
    """issue #23：单测强制限流器走进程内计数。

    开发机若在跑 Redis，login_ip 等共享 key 的计数会跨测试残留，爆破
    场景用例（SEC-05）会被历史失败计数劫持。多实例共享语义在
    test_issue23 里用 FakeRedis 双实例单独覆盖。
    """
    import backend.core.rate_limit as rl

    monkeypatch.setattr(rl, "_force_memory", True)
    rl.reset_rate_limiter()
    yield
    rl.reset_rate_limiter()


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


@pytest.fixture(autouse=True)
def _pin_embedding_stub(monkeypatch):
    """issue #78：本地 .env 的 EMBEDDING_PROVIDER 会泄漏进单测——
    与 GRAPH_DATA_MODE 泄漏同型：真实 embedding API 会限流/挂起，
    且模型锁 RT-04 会把外部模型名写进测试库。统一钉 stub。"""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "stub")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-stub-1024")
    monkeypatch.setenv("EMBEDDING_DIM", "1024")
