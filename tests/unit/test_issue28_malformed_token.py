"""issue #28：畸形 Authorization JWT 统一 401，不得 500。

- decode_token 对分段错误/非法 Base64/非法 JSON/非对象 payload/字段类型
  异常/错误签名/过期 token 全部返回 None
- 受保护接口对畸形 token 返回 401 Problem Details，响应不含内部细节
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app, reset_stores
from backend.core.config import reset_settings
from backend.core.security import _b64url, create_token, decode_token

from tests.conftest import TEST_JWT_SECRET  # 显式注入的开发密钥（issue #27）


def _sign(payload_raw: bytes, secret: str = TEST_JWT_SECRET) -> str:
    """用合法密钥对任意 payload 签名（构造「签名对但内容畸形」的 token）。"""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    sig = _b64url(_hmac(header, payload_raw, secret))
    return f"{header}.{_b64url(payload_raw)}.{sig}"


def _hmac(header: str, payload_raw: bytes, secret: str) -> bytes:
    import hashlib
    import hmac as hmac_mod

    return hmac_mod.new(
        secret.encode(), f"{header}.{_b64url(payload_raw)}".encode(),
        hashlib.sha256).digest()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    # 钉住 fixture：本地 .env 若为 live，单测会走真实 Esplora（与 test_backend_api 对齐）
    monkeypatch.setenv("GRAPH_DATA_MODE", "fixture")
    reset_settings()
    reset_stores()
    c = TestClient(create_app())
    c.headers.update({"X-Requested-With": "XMLHttpRequest"})
    yield c
    reset_settings()
    reset_stores()


class TestDecodeTokenUnit:
    def test_valid_token_roundtrip(self):
        token = create_token({"sub": "a@test"}, TEST_JWT_SECRET, 60)
        assert decode_token(token, TEST_JWT_SECRET)["sub"] == "a@test"

    def test_segment_malformed(self):
        assert decode_token("a.b.c", TEST_JWT_SECRET) is None

    def test_not_base64(self):
        # 三段但含非法 Base64 URL 字符（! @ # 等）
        assert decode_token("!!!.???.$$$", TEST_JWT_SECRET) is None

    def test_signed_but_payload_not_json(self):
        token = _sign(b"not-json-at-all")
        assert decode_token(token, TEST_JWT_SECRET) is None

    def test_signed_but_payload_not_object(self):
        # 合法签名 + 合法 JSON 但 payload 是数组/标量
        assert decode_token(_sign(json.dumps([1, 2]).encode()),
                            TEST_JWT_SECRET) is None
        assert decode_token(_sign(b"123"), TEST_JWT_SECRET) is None

    def test_signed_but_exp_not_numeric(self):
        payload = json.dumps({"sub": "a@test", "exp": "tomorrow"}).encode()
        assert decode_token(_sign(payload), TEST_JWT_SECRET) is None

    def test_wrong_signature(self):
        token = create_token({"sub": "a@test"}, TEST_JWT_SECRET, 60)
        tampered = token[:-6] + ("AAAAAA" if token[-6:] != "AAAAAA" else "BBBBBB")
        assert decode_token(tampered, TEST_JWT_SECRET) is None

    def test_expired_token(self):
        token = create_token({"sub": "a@test"}, TEST_JWT_SECRET, -10)
        assert decode_token(token, TEST_JWT_SECRET) is None

    def test_non_string_inputs(self):
        assert decode_token(None, TEST_JWT_SECRET) is None
        assert decode_token(create_token({"sub": "x"}, TEST_JWT_SECRET, 60),
                            None) is None


class TestProtectedEndpointIntegration:
    @pytest.mark.parametrize("header", [
        "Bearer a.b.c",
        "Bearer !!!.???.###",
        f"Bearer {_sign(b'not-json-at-all')}",
        f"Bearer {_sign(json.dumps([1, 2]).encode())}",
        "Bearer " + create_token({"sub": "x@test"}, TEST_JWT_SECRET, -10),
    ])
    def test_malformed_tokens_get_401(self, client, header):
        resp = client.get("/api/v1/cases", headers={"Authorization": header})
        assert resp.status_code == 401
        body = resp.text
        # 错误响应不泄露 token / 密钥 / 堆栈
        assert "Traceback" not in body
        assert TEST_JWT_SECRET not in body
        assert "not-json" not in body

    def test_no_error_monitor_pollution(self, client):
        """畸形 token 不触发 500 路径（否则会污染错误监控语义）。"""
        codes = [client.get(
            "/api/v1/cases", headers={"Authorization": "Bearer x.y.z"}
        ).status_code for _ in range(3)]
        assert codes == [401, 401, 401]
