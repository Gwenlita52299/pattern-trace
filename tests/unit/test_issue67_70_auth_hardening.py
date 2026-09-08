"""issue #67/#69 回归：token 类型隔离与停用账号的会话终止。

全部为端点级测试（issue 验收明确要求，不能只测 decode_token()）。
"""
from backend.api.app import seed_user

NON_DEMO_ADDRESS = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"


def _login(client, email, password):
    return client.post("/api/v1/auth/login",
                       json={"email": email, "password": password})


def _disable_user(admin_client, email):
    r = admin_client.patch(f"/api/v1/users/{email}?is_active=false")
    assert r.status_code == 200


class TestIssue67TokenTypeIsolation:
    """refresh token 不得作为 Bearer 通过受保护接口。"""

    def test_refresh_token_as_bearer_rejected(self, client):
        seed_user("t67@test.com", "Passw0rd!123", role="investigator")
        _login(client, "t67@test.com", "Passw0rd!123")
        refresh = client.cookies.get("refresh_token")
        client.headers.update({"Authorization": f"Bearer {refresh}"})
        r = client.get("/api/v1/cases")
        assert r.status_code == 401

    def test_rotated_refresh_as_bearer_rejected(self, client):
        # 已轮换（或已 logout 撤销）的 refresh token 同样拿不到 Bearer 待遇
        seed_user("t67r@test.com", "Passw0rd!123", role="investigator")
        _login(client, "t67r@test.com", "Passw0rd!123")
        stale = client.cookies.get("refresh_token")
        assert client.post("/api/v1/auth/refresh").status_code == 200
        client.headers.update({"Authorization": f"Bearer {stale}"})
        r = client.get("/api/v1/cases")
        assert r.status_code == 401

    def test_access_token_still_works_on_protected(self, client):
        seed_user("t67a@test.com", "Passw0rd!123", role="investigator")
        resp = _login(client, "t67a@test.com", "Passw0rd!123")
        client.headers.update(
            {"Authorization": f"Bearer {resp.json()['access_token']}"})
        assert client.get("/api/v1/cases").status_code == 200


class TestIssue69DisabledAccount:
    """停用账号不得登录、refresh 或绕过匿名限制。"""

    def test_disabled_user_cannot_login(self, admin_client, client):
        seed_user("d69@test.com", "Passw0rd!123")
        _disable_user(admin_client, "d69@test.com")
        r = _login(client, "d69@test.com", "Passw0rd!123")
        # 复用 INVALID_CREDENTIALS：与密码错误不可区分（防枚举）
        assert r.status_code == 401
        assert r.json()["error_code"] == "INVALID_CREDENTIALS"

    def test_disabled_user_cannot_refresh(self, admin_client, client):
        seed_user("d69r@test.com", "Passw0rd!123")
        _login(client, "d69r@test.com", "Passw0rd!123")
        _disable_user(admin_client, "d69r@test.com")
        r = client.post("/api/v1/auth/refresh")
        assert r.status_code == 401

    def test_disabled_user_access_token_rejected(self, admin_client, client):
        # 停用前签发的 access token 也不能继续获得授权（require_role 原有
        # 检查，此处端点级验证不被新改动破坏）。
        # 注意：admin_client 与 client 是同一实例，必须在覆盖 Authorization
        # 头之前完成 admin 停用操作
        seed_user("d69t@test.com", "Passw0rd!123", role="investigator")
        resp = _login(client, "d69t@test.com", "Passw0rd!123")
        _disable_user(admin_client, "d69t@test.com")
        client.headers.update(
            {"Authorization": f"Bearer {resp.json()['access_token']}"})
        assert client.get("/api/v1/cases").status_code == 403

    def test_disabled_user_analyze_treated_as_anonymous(
            self, admin_client, client):
        # 停用用户经 optional_user() 必须落回匿名路径：非演示地址 →
        # DEMO_ADDRESS_REQUIRED，而不是按已登录放行
        seed_user("d69a@test.com", "Passw0rd!123")
        resp = _login(client, "d69a@test.com", "Passw0rd!123")
        _disable_user(admin_client, "d69a@test.com")
        client.headers.update(
            {"Authorization": f"Bearer {resp.json()['access_token']}"})
        r = client.post("/api/v1/addresses/analyze",
                        json={"address": NON_DEMO_ADDRESS})
        assert r.status_code == 403
        assert r.json()["error_code"] == "DEMO_ADDRESS_REQUIRED"
