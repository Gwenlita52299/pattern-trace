"""管理端 provider 配置（前端「配置」页自选 provider）。

覆盖：
- 密钥加密入库、接口永不回显明文（只回 key_source/has_key）
- DB 配置**整体覆盖** env，不做逐字段合并（避免跨厂商密钥错配）
- 解密失败 / DB 不可达 → 整份回落 env（fail-open）
- live 形态下不可切到 mock（守住 #78 的「无按请求 LLM 故障注入」）
- 校验：未知 provider / 空模型 / 缺密钥 / 缺少 SECRETS_KEY / 非 admin
"""
from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from backend.api.app import (create_app, get_db_engine, reset_stores,
                             seed_user)
from backend.core.config import get_settings, reset_settings
from backend.models.base import ProviderConfig
from backend.services import provider_config as pc

SENTINEL_KEY = "sk-or-v1-SENTINEL-9f3a2bcafe"  # 明文密钥哨兵：不得出现在响应里


def _db_ready() -> bool:
    try:
        with get_db_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


requires_db = pytest.mark.skipif(not _db_ready(),
                                 reason="PostgreSQL 未运行")


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv(
        "JWT_SECRET",
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("GRAPH_DATA_MODE", "fixture")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("SECRETS_KEY", Fernet.generate_key().decode())
    reset_settings()
    reset_stores()
    pc._engine.cache_clear()
    pc.invalidate()
    yield
    reset_settings()
    reset_stores()
    pc.invalidate()
    try:
        if _db_ready():
            with Session(get_db_engine()) as session:
                session.execute(delete(ProviderConfig))
                session.commit()
    except Exception:  # noqa: BLE001 — 清理失败不该掩盖测试结论
        pass


@pytest.fixture()
def admin_client(env):
    seed_user("provider-admin@example.com", "Password123!", role="admin")
    seed_user("provider-inv@example.com", "Password123!", role="investigator")
    client = TestClient(create_app())
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    resp = client.post("/api/v1/auth/login",
                       json={"email": "provider-admin@example.com",
                             "password": "Password123!"})
    client.headers.update(
        {"Authorization": f"Bearer {resp.json()['access_token']}"})
    return client


@pytest.fixture()
def inv_client(env):
    seed_user("provider-inv2@example.com", "Password123!",
              role="investigator")
    client = TestClient(create_app())
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    resp = client.post("/api/v1/auth/login",
                       json={"email": "provider-inv2@example.com",
                             "password": "Password123!"})
    client.headers.update(
        {"Authorization": f"Bearer {resp.json()['access_token']}"})
    return client


def _with_row(monkeypatch, row: dict | None) -> None:
    monkeypatch.setattr(pc, "load_active", lambda kind=None, **kw: row)
    pc.invalidate()


# ---------------------------------------------------------------- 纯单元

def test_unknown_provider_rejected(env):
    with pytest.raises(pc.ProviderConfigError) as exc:
        pc.validate_provider("skynet")
    assert exc.value.code == "PROVIDER_UNKNOWN"


def test_mock_only_switchable_in_fixture(env):
    settings = get_settings()
    spec = pc.validate_provider("mock")
    ok, why = pc.switchable(spec, settings)
    assert ok is True                       # fixture 形态：允许（E2E 便利）
    assert why == ""

    live = settings.model_copy(update={"graph_data_mode": "live"})
    ok, why = pc.switchable(spec, live)
    assert ok is False
    assert "mock" in why

    # 真实 provider 在 live 下不受限
    assert pc.switchable(pc.validate_provider("openai"), live)[0] is True


def test_encrypt_decrypt_roundtrip(env):
    token = pc.encrypt_api_key(SENTINEL_KEY)
    assert token != SENTINEL_KEY
    assert pc.decrypt_api_key(token) == SENTINEL_KEY


def test_missing_or_invalid_secrets_key_rejected(env, monkeypatch):
    monkeypatch.setenv("SECRETS_KEY", "")
    reset_settings()
    with pytest.raises(pc.ProviderConfigError) as exc:
        pc.encrypt_api_key("x")
    assert exc.value.code == "SECRETS_KEY_MISSING"

    monkeypatch.setenv("SECRETS_KEY", "not-a-fernet-key")
    reset_settings()
    with pytest.raises(pc.ProviderConfigError) as exc:
        pc.encrypt_api_key("x")
    assert exc.value.code == "SECRETS_KEY_INVALID"


def test_no_db_row_is_env_untouched(env, monkeypatch):
    _with_row(monkeypatch, None)
    base = get_settings()
    assert pc.effective_llm_settings() is base


def test_row_overlays_all_llm_fields(env, monkeypatch):
    _with_row(monkeypatch, {
        "provider": "openai", "model": "gpt-4o-mini",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_encrypted": pc.encrypt_api_key(SENTINEL_KEY),
        "updated_by": "a@b.c", "updated_at": None})
    eff = pc.effective_llm_settings()
    assert eff.llm_provider == "openai"
    assert eff.llm_model == "gpt-4o-mini"
    assert eff.llm_base_url == "https://openrouter.ai/api/v1"
    assert eff.llm_api_key == SENTINEL_KEY
    # 其余字段仍是 env 原样（只覆盖 llm_*）
    assert eff.graph_data_mode == get_settings().graph_data_mode
    assert get_settings().llm_provider == "mock"   # 单例未被就地改写


def test_empty_base_url_clears_env_url(env, monkeypatch):
    """切换 provider 时不能把上一个 provider 的 env base_url 带过去。"""
    _with_row(monkeypatch, {"provider": "openai", "model": "gpt-4o-mini",
                            "base_url": "", "api_key_encrypted": "",
                            "updated_by": None, "updated_at": None})
    assert pc.effective_llm_settings().llm_base_url == ""


def test_undecryptable_key_falls_back_to_env_entirely(env, monkeypatch):
    """解不开就整份回落：宁可用旧 provider，也不把 env 密钥配到 DB 的 provider。"""
    other = Fernet.generate_key().decode()
    from cryptography.fernet import Fernet as F
    token = F(other.encode()).encrypt(b"secret").decode()
    _with_row(monkeypatch, {"provider": "openai", "model": "gpt-4o-mini",
                            "base_url": "https://x", "api_key_encrypted": token,
                            "updated_by": None, "updated_at": None})
    eff = pc.effective_llm_settings()
    assert eff.llm_provider == "mock"        # 回落 env，而非 openai
    assert eff.llm_api_key == ""


def test_load_active_fails_open_when_db_unavailable(env, monkeypatch):
    def _boom(kind):
        raise RuntimeError("db down")

    monkeypatch.setattr(pc, "_read_row", _boom)
    pc.invalidate()
    assert pc.load_active(pc.LLM_KIND, ttl=0) is None
    assert pc.effective_llm_settings().llm_provider == "mock"


# ---------------------------------------------------------------- API 端点

@requires_db
def test_put_then_get_never_echoes_plaintext_key(admin_client):
    resp = admin_client.put("/api/v1/admin/provider-config", json={
        "provider": "openai", "model": "gpt-4o-mini",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": SENTINEL_KEY})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"]["provider"] == "openai"
    assert body["active"]["model"] == "gpt-4o-mini"
    assert body["active"]["source"] == "database"
    assert body["active"]["key_source"] == "database"
    assert body["active"]["has_key"] is True
    assert SENTINEL_KEY not in resp.text           # 明文密钥绝不回显
    assert "api_key" not in body["active"]         # active 里没有密钥字段
    assert "api_key_encrypted" not in json.dumps(body)   # 密文同样不出接口

    listed = admin_client.get("/api/v1/admin/provider-config")
    assert listed.status_code == 200
    assert SENTINEL_KEY not in listed.text
    assert listed.json()["active"]["source"] == "database"

    # 密文确实落库、且不是明文
    with Session(get_db_engine()) as session:
        row = session.query(ProviderConfig).filter_by(kind="llm").one()
        assert row.api_key_encrypted and SENTINEL_KEY not in row.api_key_encrypted


@requires_db
def test_saved_config_is_effective_immediately(admin_client):
    admin_client.put("/api/v1/admin/provider-config", json={
        "provider": "openai", "model": "gpt-4o-mini",
        "base_url": "https://openrouter.ai/api/v1", "api_key": SENTINEL_KEY})
    eff = pc.effective_llm_settings()        # 写入进程已 immediately 失效缓存
    assert eff.llm_provider == "openai"
    assert eff.llm_model == "gpt-4o-mini"
    assert eff.llm_base_url == "https://openrouter.ai/api/v1"


@requires_db
def test_reset_falls_back_to_env(admin_client):
    admin_client.put("/api/v1/admin/provider-config", json={
        "provider": "openai", "model": "gpt-4o-mini", "api_key": SENTINEL_KEY})
    resp = admin_client.delete("/api/v1/admin/provider-config")
    assert resp.status_code == 200
    assert resp.json()["active"]["source"] == "environment"
    assert resp.json()["active"]["provider"] == "mock"
    assert pc.effective_llm_settings().llm_provider == "mock"


@requires_db
def test_clear_api_key_falls_back_to_env(admin_client, monkeypatch):
    """清除 = 回落环境变量；env 有密钥时允许，且 DB 密文确实被清掉。"""
    monkeypatch.setenv("LLM_API_KEY", "env-provided-key")
    reset_settings()
    admin_client.put("/api/v1/admin/provider-config", json={
        "provider": "openai", "model": "gpt-4o-mini", "api_key": SENTINEL_KEY})
    resp = admin_client.put("/api/v1/admin/provider-config", json={
        "provider": "openai", "model": "gpt-4o-mini", "clear_api_key": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["active"]["key_source"] == "environment"
    with Session(get_db_engine()) as session:
        row = session.query(ProviderConfig).filter_by(kind="llm").one()
        assert not row.api_key_encrypted
        assert row.provider == "openai"          # 只清密钥，不动 provider/model


@requires_db
def test_clear_api_key_rejected_when_env_has_none(admin_client):
    """env 也没有密钥时，清掉就等于存一个必然失败的配置——保存时就拒绝。"""
    admin_client.put("/api/v1/admin/provider-config", json={
        "provider": "openai", "model": "gpt-4o-mini", "api_key": SENTINEL_KEY})
    resp = admin_client.put("/api/v1/admin/provider-config", json={
        "provider": "openai", "model": "gpt-4o-mini", "clear_api_key": True})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "PROVIDER_KEY_REQUIRED"


@requires_db
def test_keeps_existing_key_when_omitted(admin_client):
    admin_client.put("/api/v1/admin/provider-config", json={
        "provider": "openai", "model": "gpt-4o-mini", "api_key": SENTINEL_KEY})
    resp = admin_client.put("/api/v1/admin/provider-config", json={
        "provider": "openai", "model": "gpt-4o"})
    assert resp.status_code == 200
    assert resp.json()["active"]["key_source"] == "database"   # 密钥被保留


@requires_db
def test_requires_key_when_nothing_available(admin_client):
    resp = admin_client.put("/api/v1/admin/provider-config", json={
        "provider": "openai", "model": "gpt-4o-mini"})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "PROVIDER_KEY_REQUIRED"


@requires_db
def test_missing_secrets_key_blocks_saving_key(admin_client, monkeypatch):
    monkeypatch.setenv("SECRETS_KEY", "")
    reset_settings()
    resp = admin_client.put("/api/v1/admin/provider-config", json={
        "provider": "openai", "model": "gpt-4o-mini", "api_key": SENTINEL_KEY})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "SECRETS_KEY_MISSING"


@requires_db
def test_rejects_unknown_provider_and_empty_model(admin_client):
    resp = admin_client.put("/api/v1/admin/provider-config", json={
        "provider": "skynet", "model": "x", "api_key": SENTINEL_KEY})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "PROVIDER_UNKNOWN"

    resp = admin_client.put("/api/v1/admin/provider-config", json={
        "provider": "openai", "model": "   ", "api_key": SENTINEL_KEY})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


@requires_db
def test_mock_not_switchable_in_live(tmp_path, monkeypatch):
    """live 形态切到 mock 会重开 #78 关掉的按请求故障注入通道。"""
    monkeypatch.setenv("GRAPH_DATA_MODE", "live")
    reset_settings()
    seed_user("provider-admin2@example.com", "Password123!", role="admin")
    client = TestClient(create_app())
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    resp = client.post("/api/v1/auth/login",
                       json={"email": "provider-admin2@example.com",
                             "password": "Password123!"})
    client.headers.update(
        {"Authorization": f"Bearer {resp.json()['access_token']}"})
    resp = client.put("/api/v1/admin/provider-config", json={
        "provider": "mock", "model": "mock"})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "PROVIDER_NOT_SWITCHABLE"

    listed = client.get("/api/v1/admin/provider-config").json()
    mock_item = next(p for p in listed["providers"] if p["name"] == "mock")
    assert mock_item["switchable"] is False


@requires_db
def test_ambiguous_key_intent_rejected(admin_client):
    resp = admin_client.put("/api/v1/admin/provider-config", json={
        "provider": "openai", "model": "gpt-4o", "api_key": SENTINEL_KEY,
        "clear_api_key": True})
    assert resp.status_code == 422


@requires_db
def test_investigator_forbidden(inv_client):
    assert inv_client.get(
        "/api/v1/admin/provider-config").status_code == 403
    assert inv_client.put("/api/v1/admin/provider-config", json={
        "provider": "openai", "model": "x"}).status_code == 403
    assert inv_client.delete(
        "/api/v1/admin/provider-config").status_code == 403


@requires_db
def test_describe_lists_registry_providers(admin_client):
    body = admin_client.get("/api/v1/admin/provider-config").json()
    names = {p["name"] for p in body["providers"]}
    assert {"openai", "anthropic", "vllm", "deepseek", "ollama", "mock"} <= names
    openai = next(p for p in body["providers"] if p["name"] == "openai")
    assert openai["requires_api_key"] is True
    assert "structured_output" in openai
    assert body["secrets_key_configured"] is True
