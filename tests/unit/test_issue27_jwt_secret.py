"""issue #27：JWT_SECRET 必填 + 强度校验，缺失/占位/弱熵拒绝启动。

- 运行时不再有 setdefault 弱默认：缺失密钥 → Settings 校验明确失败
- 占位值（dev-secret / change-me-... / placeholder）拒绝
- 长度 < 32、distinct 字符 < 10 拒绝
- 合法随机密钥正常加载
"""
from __future__ import annotations

import pytest
from pydantic_core import ValidationError

from backend.core.config import Settings, reset_settings

TEST_SECRET = "0123456789abcdef" * 4  # 与 conftest 注入的开发密钥一致


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """隔离 .env 文件与既有 JWT_SECRET：脱离项目目录避免 env_file 干扰。"""
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_settings()
    yield
    reset_settings()


class TestMissingSecret:
    def test_missing_jwt_secret_fails_to_load(self):
        with pytest.raises(ValidationError, match="jwt_secret"):
            Settings()

    def test_get_settings_fails_without_secret(self):
        from backend.core.config import get_settings

        with pytest.raises(ValidationError):
            get_settings()


class TestWeakSecretsRejected:
    def test_placeholder_value_rejected(self):
        with pytest.raises(ValidationError, match="placeholder"):
            Settings(jwt_secret="change-me-to-a-long-random-string")

    def test_dev_secret_rejected(self):
        # dev-secret 补足到 32 字符后仍应命中占位名单
        with pytest.raises(ValidationError, match="placeholder"):
            Settings(jwt_secret="dev-secret-0123456789abcdef0123456789abcdef")

    def test_short_secret_rejected(self):
        with pytest.raises(ValidationError, match="at least 32"):
            Settings(jwt_secret="ci-secret")

    def test_low_entropy_secret_rejected(self):
        with pytest.raises(ValidationError, match="entropy"):
            Settings(jwt_secret="a" * 64)

    def test_repeated_pattern_secret_rejected(self):
        with pytest.raises(ValidationError, match="entropy"):
            Settings(jwt_secret="abc" * 22)


class TestValidSecretAccepted:
    def test_valid_random_secret_loads(self):
        settings = Settings(jwt_secret=TEST_SECRET)
        assert settings.jwt_secret == TEST_SECRET

    def test_no_runtime_default_injection(self):
        """get_settings 不再回填任何默认值：缺失即失败，而非静默 dev-secret。"""
        from backend.core.config import get_settings

        try:
            get_settings()
        except ValidationError:
            pass  # 期望路径：缺失 → 校验错误
        else:
            pytest.fail("get_settings() loaded without JWT_SECRET")
