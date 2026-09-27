"""管理端 provider 配置——前端「配置」页自选 provider。

三件事，以及它们各自的取舍：

1. **加密存储**：API Key 用 Fernet（AES-CBC + HMAC 认证加密）落库，钥匙来自
   `SECRETS_KEY`。接口永不回显明文，只回 `key_source` / `has_key`——面板
   刷新后看到「已配置」而不是把密钥重新打到屏幕上。
2. **整体覆盖，不做逐字段合并**：DB 有配置时，provider/model/base_url/api_key
   一起覆盖 env。逐字段合并是陷阱：把 DB 里的 openai 配上 env 里的 DeepSeek
   密钥，请求会带着 A 家密钥打到 B 家（401 起步）。要么整份用 DB，要么整份用 env。
3. **跨进程生效**：API 与 worker 是不同进程，写入方无法让读方缓存失效，
   所以读侧用 5s TTL（写入方额外立即失效**本进程**缓存）——"立即生效"的
   真实语义是"≤5s 内生效"，这一点写进 spec，不假装是毫秒级。

失败方向一律是 **fail-open 回落 env**：DB 不可达、密钥解不开、行数据不完整，
都退回 env 配置并 print 留痕。配置读取不该成为分析链路的单点故障——退一步说，
用 env 的旧 provider 至少是「能跑但可能不是刚改的那份」，比整条链路 500 好。
"""
from __future__ import annotations

import threading
import time
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings, get_settings
from ..core.providers.registry import REGISTRY, ProviderSpec
from ..models.base import ProviderConfig

__all__ = [
    "LLM_KIND", "ProviderConfigError", "encrypt_api_key", "decrypt_api_key",
    "load_active", "effective_llm_settings", "invalidate", "describe", "update",
    "switchable",
    "reset", "validate_provider", "validate_secrets_key",
]

LLM_KIND = "llm"

# 读侧缓存 TTL：跨进程生效的最坏延迟（写入进程立即失效自己的缓存）
CACHE_TTL_SECONDS = 5.0

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict | None]] = {}
_fallback_logged = False


class ProviderConfigError(Exception):
    """配置校验失败（由 API 层映射为 4xx，不产生 5xx）。"""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ------------------------------------------------------------------ 加密

@lru_cache(maxsize=1)
def _engine():
    from sqlalchemy import create_engine

    return create_engine(
        get_settings().database_url.replace(
            "postgresql://", "postgresql+psycopg://"),
        pool_pre_ping=True)


def validate_secrets_key(settings: Settings | None = None) -> str:
    """校验 SECRETS_KEY 是合法 Fernet 钥匙；缺失/非法都明确报错。

    缺失时**不**回落成某个默认值或自动生成——自动生成的钥匙会在下次重启时
    消失，把已存的密文变成永久解不开的垃圾；明确拒绝比静默损坏更有用。
    """
    settings = settings or get_settings()
    key = (settings.secrets_key or "").strip()
    if not key:
        raise ProviderConfigError(
            "SECRETS_KEY_MISSING",
            "服务端未配置 SECRETS_KEY，无法保存 API Key（可设置环境变量后重试；"
            "用 openssl rand -base64 32 生成）",
            422)
    try:
        Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise ProviderConfigError(
            "SECRETS_KEY_INVALID",
            f"SECRETS_KEY 不是合法的 Fernet 钥匙（需 44 字符 urlsafe-base64）: {exc}",
            422)
    return key


def encrypt_api_key(plain: str, settings: Settings | None = None) -> str:
    key = validate_secrets_key(settings)
    return Fernet(key.encode()).encrypt(plain.encode()).decode()


def decrypt_api_key(token: str, settings: Settings | None = None) -> str:
    """解不开就抛——调用方据此整体回落 env，而不是带着半个配置继续跑。"""
    key = validate_secrets_key(settings)
    try:
        return Fernet(key.encode()).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, TypeError) as exc:
        raise ProviderConfigError(
            "SECRETS_KEY_MISMATCH",
            "已存密钥无法解密（SECRETS_KEY 与写入时不一致？）", 500) from exc


# ------------------------------------------------------------------ 读取

def _warn_fallback(detail: str) -> None:
    """留痕一次即可，避免每次分析都刷屏。"""
    global _fallback_logged
    with _lock:
        if _fallback_logged:
            return
        _fallback_logged = True
    print(f"[provider_config] falling back to env config ({detail})")


def _read_row(kind: str) -> dict | None:
    with Session(_engine()) as session:
        row = session.execute(
            select(ProviderConfig).where(ProviderConfig.kind == kind)
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "provider": row.provider,
            "model": row.model,
            "base_url": row.base_url or "",
            "api_key_encrypted": row.api_key_encrypted or "",
            "updated_by": row.updated_by,
            "updated_at": (row.updated_at or row.created_at).isoformat()
            if (row.updated_at or row.created_at) else None,
        }


def load_active(kind: str = LLM_KIND, *, ttl: float = CACHE_TTL_SECONDS,
                now: float | None = None) -> dict | None:
    """当前生效的 DB 配置快照（无行 = None）；异常一律视作「没有 DB 配置」。"""
    clock = time.monotonic() if now is None else now
    with _lock:
        cached = _cache.get(kind)
        if cached is not None and cached[0] > clock and ttl > 0:
            return cached[1]
    try:
        snapshot = _read_row(kind)
    except Exception as exc:  # noqa: BLE001 — fail-open，见模块 docstring
        _warn_fallback(f"{kind}: {exc.__class__.__name__}")
        snapshot = None
    with _lock:
        _cache[kind] = (clock + ttl, snapshot)
    return snapshot


def invalidate(kind: str | None = None) -> None:
    """写入后清缓存（本进程立即生效；其他进程等 TTL 到点）。"""
    with _lock:
        if kind is None:
            _cache.clear()
        else:
            _cache.pop(kind, None)


def effective_llm_settings(base: Settings | None = None) -> Settings:
    """env settings 的副本，DB 有配置时整体覆盖 llm_* 四项。

    返回副本而非就地改 settings：`get_settings()` 是进程级单例，就地改写会
    污染所有调用方（含只用 env 的工具与测试）。
    """
    settings = base or get_settings()
    row = load_active(LLM_KIND)
    if not row:
        return settings
    if not row.get("provider") or not row.get("model"):
        _warn_fallback("row missing provider/model")
        return settings

    api_key = settings.llm_api_key
    if row.get("api_key_encrypted"):
        try:
            api_key = decrypt_api_key(row["api_key_encrypted"], settings)
        except ProviderConfigError as exc:
            # 解不开就整份回落 env：宁可用旧 provider，也不把 env 密钥
            # 配到 DB 的 provider 上（跨厂商密钥错配）
            _warn_fallback(f"api key undecryptable: {exc.code}")
            return settings

    return settings.model_copy(update={
        "llm_provider": row["provider"],
        "llm_model": row["model"],
        # 空 = 交给 provider 工厂用其内置默认地址（避免把上一个 provider
        # 的 env base_url 带过去）
        "llm_base_url": row.get("base_url") or "",
        "llm_api_key": api_key,
    })


# ------------------------------------------------------------------ 校验/写入

def validate_provider(name: str) -> ProviderSpec:
    try:
        return REGISTRY.get(LLM_KIND, name)
    except Exception as exc:  # noqa: BLE001 — 统一转成配置错误（4xx）
        raise ProviderConfigError(
            "PROVIDER_UNKNOWN", f"未知的 provider: {name}", 422) from exc


def switchable(spec: ProviderSpec, settings: Settings) -> tuple[bool, str]:
    """面板可切换性：live 形态下不允许切到 mock。

    #78 的不变量是「生产/live 形态不存在按请求注入 LLM 故障的接口」，而
    mock_scenario 通道只在 llm_provider == mock 时生效——若运行时可切到
    mock，等于把那条被刻意关掉的通道重新打开。env 方式不受此限：那是部署时
    的运维决定，不是运行时可被 API 调用的开关。
    """
    if spec.name != "mock":
        return True, ""
    if settings.graph_data_mode == "fixture":
        return True, ""
    return False, "live 形态下不可切换到 mock（会重新打开按请求注入 LLM 故障的通道）"


def _key_source(spec: ProviderSpec, row: dict | None, api_key: str) -> str:
    if row and row.get("api_key_encrypted"):
        return "database"
    if api_key:
        return "environment"
    if not spec.capabilities.requires_api_key:
        return "not_required"
    return "none"


def describe(settings: Settings | None = None) -> dict:
    """面板渲染所需的全部信息（含注册表清单供下拉）。"""
    settings = settings or get_settings()
    row = load_active(LLM_KIND)
    effective = effective_llm_settings(settings)
    spec = validate_provider(effective.llm_provider)
    providers = []
    for spec_item in REGISTRY.specs(LLM_KIND):
        item = {"name": spec_item.name, "display": spec_item.display}
        item.update(spec_item.capabilities.as_dict())
        ok, why = switchable(spec_item, settings)
        item["switchable"] = ok
        item["note"] = why
        providers.append(item)
    return {
        "kind": LLM_KIND,
        "active": {
            "provider": effective.llm_provider,
            "model": effective.llm_model,
            "base_url": effective.llm_base_url,
            "source": "database" if row else "environment",
            "key_source": _key_source(spec, row, effective.llm_api_key),
            "has_key": bool(effective.llm_api_key)
            or not spec.capabilities.requires_api_key,
            "updated_by": (row or {}).get("updated_by"),
            "updated_at": (row or {}).get("updated_at"),
        },
        "secrets_key_configured": bool((settings.secrets_key or "").strip()),
        "providers": providers,
    }


def update(provider: str, model: str, *, base_url: str | None = None,
           api_key: str | None = None, clear_api_key: bool = False,
           actor_id: str | None = None,
           settings: Settings | None = None) -> dict:
    """保存配置（upsert）。api_key 省略 = 保留既有密文。"""
    settings = settings or get_settings()
    spec = validate_provider(provider)
    ok, why = switchable(spec, settings)
    if not ok:
        raise ProviderConfigError("PROVIDER_NOT_SWITCHABLE", why, 422)

    model = (model or "").strip()
    if not model:
        raise ProviderConfigError("VALIDATION_ERROR", "模型 ID 不能为空", 422)
    base_url = (base_url or "").strip()

    row = load_active(LLM_KIND)
    if api_key:
        ciphertext = encrypt_api_key(api_key, settings)
    elif clear_api_key:
        ciphertext = None
    else:
        ciphertext = (row or {}).get("api_key_encrypted") or None

    # 最终密钥：明文优先（本次新填），否则 DB 里的密文存在即视为有密钥
    has_key = bool(api_key) or bool(ciphertext) or bool(settings.llm_api_key)
    if spec.capabilities.requires_api_key and not has_key:
        raise ProviderConfigError(
            "PROVIDER_KEY_REQUIRED",
            f"provider {provider} 需要 API Key（面板留空时回落环境变量 "
            "LLM_API_KEY，两者都没有则无法保存）", 422)

    with Session(_engine()) as session:
        existing = session.execute(
            select(ProviderConfig).where(ProviderConfig.kind == LLM_KIND)
        ).scalar_one_or_none()
        if existing is None:
            session.add(ProviderConfig(
                kind=LLM_KIND, provider=provider, model=model,
                base_url=base_url, api_key_encrypted=ciphertext,
                updated_by=actor_id))
        else:
            existing.provider = provider
            existing.model = model
            existing.base_url = base_url
            existing.api_key_encrypted = ciphertext
            existing.updated_by = actor_id
        session.commit()

    invalidate(LLM_KIND)
    return describe(settings)


def reset(settings: Settings | None = None) -> dict:
    """删除 DB 配置 → 回落 env。没有这条路径，面板就成了单向门。"""
    with Session(_engine()) as session:
        row = session.execute(
            select(ProviderConfig).where(ProviderConfig.kind == LLM_KIND)
        ).scalar_one_or_none()
        if row is not None:
            session.delete(row)
            session.commit()
    invalidate(LLM_KIND)
    return describe(settings)
