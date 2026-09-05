"""issue #23 限流测试：登录爆破防护（SEC-05）+ 多实例共享计数。

- 登录失败按 IP 与账号双维度限流，429 + Retry-After，响应不泄露用户存在性
- 成功登录清零计数
- refresh 等认证路径 IP 限流
- RedisRateLimiter 多实例共享计数（FakeRedis 模拟两个 API 进程）
- Redis 不可达 fail-open 降级进程内计数
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app, reset_stores, seed_user
from backend.core.config import reset_settings
from backend.core.rate_limit import RedisRateLimiter

CSRF = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture()
def api_client(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.delenv("LLM_MOCK_SCENARIO", raising=False)
    reset_settings()
    reset_stores()
    client = TestClient(create_app())
    client.headers.update(CSRF)
    yield client
    reset_settings()
    reset_stores()


class TestLoginBruteForce:
    """SEC-05：60s 窗口内 10 次失败后，第 11 次（即使密码正确）→ 429。"""

    def test_11th_attempt_429_with_retry_after(self, api_client):
        seed_user("inv@test.com", "Passw0rd!123")
        # 10 次错误密码（401，消息一致防枚举）
        for _ in range(10):
            r = api_client.post("/api/v1/auth/login",
                                json={"email": "inv@test.com",
                                      "password": "wrong"})
            assert r.status_code == 401
            assert r.json()["detail"] == "Invalid credentials"
        # 第 11 次用正确密码：IP 维度已超限 → 429 + Retry-After
        r = api_client.post("/api/v1/auth/login",
                            json={"email": "inv@test.com",
                                  "password": "Passw0rd!123"})
        assert r.status_code == 429
        assert r.headers.get("Retry-After") is not None
        assert r.json()["error_code"] == "RATE_LIMITED"

    def test_unknown_email_brute_forced_same_message(self, api_client):
        """爆破不存在的邮箱：401 消息与存在账号完全一致（防枚举）。"""
        for _ in range(10):
            r = api_client.post("/api/v1/auth/login",
                                json={"email": "ghost@test.com",
                                      "password": "wrong"})
            assert r.status_code == 401
            assert r.json()["detail"] == "Invalid credentials"
        r = api_client.post("/api/v1/auth/login",
                            json={"email": "ghost@test.com",
                                  "password": "wrong"})
        assert r.status_code == 429

    def test_success_login_resets_counters(self, api_client):
        seed_user("inv@test.com", "Passw0rd!123")
        for _ in range(5):
            api_client.post("/api/v1/auth/login",
                            json={"email": "inv@test.com",
                                  "password": "wrong"})
        # 未超限：正确密码成功，计数清零
        r = api_client.post("/api/v1/auth/login",
                            json={"email": "inv@test.com",
                                  "password": "Passw0rd!123"})
        assert r.status_code == 200
        # 清零后重新数满 10 次失败才 429
        for _ in range(10):
            api_client.post("/api/v1/auth/login",
                            json={"email": "inv@test.com",
                                  "password": "wrong"})
        r = api_client.post("/api/v1/auth/login",
                            json={"email": "inv@test.com",
                                  "password": "Passw0rd!123"})
        assert r.status_code == 429

    def test_email_dimension_independent_of_ip(self, api_client, monkeypatch):
        """另一账号不受同 IP 下别人失败计数影响（IP 未超限时）。"""
        seed_user("other@test.com", "Passw0rd!123")
        for _ in range(5):
            api_client.post("/api/v1/auth/login",
                            json={"email": "victim@test.com",
                                  "password": "wrong"})
        r = api_client.post("/api/v1/auth/login",
                            json={"email": "other@test.com",
                                  "password": "Passw0rd!123"})
        assert r.status_code == 200


class TestRefreshRateLimit:
    def test_refresh_ip_rate_limited(self, api_client, monkeypatch):
        monkeypatch.setenv("AUTH_RATE_PER_MIN", "2")
        reset_settings()
        reset_stores()
        client = TestClient(create_app())
        client.headers.update(CSRF)
        codes = [client.post("/api/v1/auth/refresh").status_code
                 for _ in range(4)]
        # 前两次进入正常流程（无有效 token → 401），第 3 次起 429
        assert codes[:2] == [401, 401]
        assert codes[2] == 429 and codes[3] == 429


# ---------------------------------------------------------------------------
# RedisRateLimiter 单元：多实例共享 + fail-open 降级
# ---------------------------------------------------------------------------
class FakeRedis:
    """最小 redis 语义（get/incr/expire/ttl/delete），计数永不自然过期。"""

    def __init__(self):
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key):
        return str(self.store[key]).encode() if key in self.store else None

    def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key, ttl):
        self.ttls[key] = ttl

    def ttl(self, key):
        return self.ttls.get(key, -1)

    def delete(self, key):
        self.store.pop(key, None)
        self.ttls.pop(key, None)


class TestSharedCounting:
    def test_two_instances_share_counters(self, monkeypatch):
        """模拟两个 API 进程：实例 A 记满失败，实例 B 的 check 即被拒。"""
        fake = FakeRedis()
        instance_a = RedisRateLimiter(forced_memory=True)
        instance_a._redis = fake
        instance_b = RedisRateLimiter(forced_memory=True)
        instance_b._redis = fake

        for _ in range(10):
            allowed, _ = instance_a.hit("login_ip", "1.2.3.4",
                                        limit=10, window=60)
            assert allowed
        allowed_b, retry_after = instance_b.check(
            "login_ip", "1.2.3.4", limit=10)
        assert not allowed_b
        assert retry_after == 60

        # 任意一实例 reset，另一实例同步放行
        instance_b.reset("login_ip", "1.2.3.4")
        allowed_a, _ = instance_a.check("login_ip", "1.2.3.4", limit=10)
        assert allowed_a

    def test_fail_open_memory_mode_enforces_per_process(self):
        """Redis 不可达（无 _redis）：fail-open 降级进程内仍执行限流。"""
        limiter = RedisRateLimiter(redis_url="", forced_memory=True)
        assert limiter._redis is None
        results = [limiter.hit("anon_rate", "ip-x", limit=3, window=60)[0]
                   for _ in range(5)]
        assert results == [True, True, True, False, False]
