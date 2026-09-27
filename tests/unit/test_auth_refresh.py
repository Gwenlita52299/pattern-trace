"""JWT access + refresh rotation — 阶段0 完成标志验证."""
import time

from backend.core.security import create_token, decode_token


class TestRefreshEndpoint:
    def test_login_delivers_refresh_via_httponly_cookie_only(self, client):
        from backend.api.app import seed_user
        seed_user("t@test.com", "Passw0rd!123")
        resp = client.post("/api/v1/auth/login",
                           json={"email": "t@test.com", "password": "Passw0rd!123"})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        # D4：refresh token 绝不出现在响应 body，只经 HttpOnly Cookie 下发
        assert "refresh_token" not in data
        assert "refresh_token" in client.cookies

    def test_refresh_returns_role_for_admin_entry_visibility(self, client):
        """硬刷新后前端要恢复 admin 入口显隐，refresh 必须带 user.role。

        回归：此前 refresh 只回 email/access_token，整页刷新后 authStore.role
        为 null，PatternLifecycle / 配置页等 admin 入口会凭空消失。
        """
        from backend.api.app import seed_user
        seed_user("role-admin@test.com", "Passw0rd!123", role="admin")
        client.post("/api/v1/auth/login",
                    json={"email": "role-admin@test.com",
                          "password": "Passw0rd!123"})
        resp = client.post("/api/v1/auth/refresh")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("user", {}).get("role") == "admin"
        assert data["email"] == "role-admin@test.com"

    def test_refresh_rotation(self, client):
        from backend.api.app import seed_user
        seed_user("r@test.com", "Passw0rd!123")
        resp = client.post("/api/v1/auth/login",
                           json={"email": "r@test.com", "password": "Passw0rd!123"})
        old_access = resp.json()["access_token"]

        # 浏览器路径：无 body，凭 HttpOnly Cookie 轮换
        resp2 = client.post("/api/v1/auth/refresh")
        assert resp2.status_code == 200
        new = resp2.json()
        assert "refresh_token" not in new
        assert new["access_token"] != old_access
        assert client.cookies.get("refresh_token")

    def test_refresh_reuse_detected(self, client):
        from backend.api.app import seed_user
        seed_user("u@test.com", "Passw0rd!123")
        resp = client.post("/api/v1/auth/login",
                           json={"email": "u@test.com", "password": "Passw0rd!123"})
        stolen = client.cookies.get("refresh_token")

        # First rotation succeeds
        r1 = client.post("/api/v1/auth/refresh")
        assert r1.status_code == 200

        # Replaying the pre-rotation token → reuse detected（模拟被窃取重放）
        r2 = client.post("/api/v1/auth/refresh",
                         json={"refresh_token": stolen})
        assert r2.status_code == 401

    def test_access_token_works_on_protected(self, client):
        from backend.api.app import seed_user
        seed_user("p@test.com", "Passw0rd!123", role="investigator")
        resp = client.post("/api/v1/auth/login",
                           json={"email": "p@test.com", "password": "Passw0rd!123"})
        token = resp.json()["access_token"]
        client.headers.update({"Authorization": f"Bearer {token}"})

        cases = client.get("/api/v1/cases")
        assert cases.status_code == 200

    def test_no_token_rejected_on_protected(self, client):
        resp = client.get("/api/v1/cases")
        assert resp.status_code == 401

    def test_expired_refresh_rejected(self, client):
        settings_jwt = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        expired = create_token(
            {"sub": "x@test", "jti": "old", "family": "fam", "typ": "refresh"},
            settings_jwt, -10,
        )
        resp = client.post("/api/v1/auth/refresh",
                           json={"refresh_token": expired})
        assert resp.status_code == 401

    def test_access_token_cannot_be_used_as_refresh(self, client):
        from backend.api.app import seed_user
        seed_user("a@test.com", "Passw0rd!123")
        resp = client.post("/api/v1/auth/login",
                           json={"email": "a@test.com", "password": "Passw0rd!123"})
        access = resp.json()["access_token"]
        r = client.post("/api/v1/auth/refresh", json={"refresh_token": access})
        assert r.status_code == 401  # typ != refresh
