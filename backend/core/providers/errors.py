"""统一 Provider 错误分类 — issue #76。

LLM / embedding（以及未来的 reranker）共用同一套错误码。此前只有两个
散落的映射函数（orchestration._provider_error_code、builder._esplora_error_code），
认证失败与响应畸形无处归类：401 会落进通用的 PROVIDER_ERROR，响应畸形
被当作校验失败，二者在重试策略上的处理也完全不同。

Esplora 数据源保留自己的一套（retry/breaker/budget），但语义与本枚举
对齐，见 graph_builder.builder._esplora_error_code。

分类原则：只把**可归因于 provider 层**的异常收敛成 ProviderError；
代码 bug（如 TypeError/AttributeError）原样传播——把它们伪装成
"上游不可用"会掩盖缺陷并触发无意义重试。
"""
from __future__ import annotations

import enum

__all__ = [
    "ProviderErrorCode", "ProviderError", "RETRYABLE_CODES", "classify",
]


class ProviderErrorCode(str, enum.Enum):
    TIMEOUT = "timeout"                  # 请求/连接超时
    RATE_LIMITED = "rate_limited"        # 429（含本地并发闸门快速失败）
    AUTH_FAILED = "auth_failed"          # 401/403
    # 402 / 429+insufficient_quota：账户余额或配额耗尽。与 RATE_LIMITED 分开，
    # 因为处置完全不同——限流是等一会儿重试，余额不足是要充值或换 key
    QUOTA_EXCEEDED = "quota_exceeded"
    INVALID_RESPONSE = "invalid_response"  # 响应畸形/缺字段/请求被拒
    UNAVAILABLE = "unavailable"          # 5xx / 连接失败 / 上游不可达
    CONFIGURATION = "configuration"      # 缺 key、base_url 非法等本地配置问题


# 只有这三类值得重试：配置错误重试一万次也不会变好，认证失败更是。
# quota_exceeded 同样不重试——充值前重试只是把"余额不足"重复若干遍，
# 徒增延迟与上游噪音。
RETRYABLE_CODES = frozenset({
    ProviderErrorCode.TIMEOUT,
    ProviderErrorCode.RATE_LIMITED,
    ProviderErrorCode.UNAVAILABLE,
})


class ProviderError(RuntimeError):
    """统一 provider 异常；`code` 是唯一对外契约（health / error_code 映射）。"""

    def __init__(self, code: ProviderErrorCode, message: str, *,
                 provider: str = "", model: str = "", kind: str = "",
                 status: int | None = None,
                 retryable: bool | None = None,
                 cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.provider = provider
        self.model = model
        self.kind = kind  # "llm" | "embedding"：编排层据此选 error_code 前缀
        self.status = status
        self.retryable = (code in RETRYABLE_CODES
                          if retryable is None else retryable)
        if cause is not None:
            self.__cause__ = cause


_QUOTA_HINTS = (
    "insufficient_quota",           # OpenAI 系：429 + error.code
    "insufficient_user_quota",      # OrcaRouter 等网关：402 + error.code
    "exceeded your current quota",
    "out of credits",
    "insufficient credit",
)


def _looks_like_quota(exc: BaseException) -> bool:
    """429 里区分"限流"与"额度耗尽"：前者退避重试，后者重试无意义。

    OpenAI 系用 429 + `insufficient_quota` 表达余额耗尽，与真正的限流同状态码，
    但一个该退避重试、一个该去充值——只看状态码会把后者变成一轮无用的重试。
    只读响应体里的错误码/固定短语，不做宽泛的关键词匹配。
    """
    resp = getattr(exc, "response", None)
    text = (getattr(resp, "text", "") or "")[:2000].lower()
    return any(h in text for h in _QUOTA_HINTS)


def _status_of(exc: BaseException) -> int | None:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None)


def classify(exc: BaseException, *, provider: str = "",
             model: str = "", kind: str = "") -> ProviderError | None:
    """把上游异常收敛为统一分类。

    返回 None 表示这不是 provider 层错误（编程缺陷/业务异常），调用方
    应原样上抛。已是 ProviderError 时直接补全 provider/model/kind 返回。
    """
    if isinstance(exc, ProviderError):
        if provider and not exc.provider:
            exc.provider = provider
        if model and not exc.model:
            exc.model = model
        if kind and not exc.kind:
            exc.kind = kind
        return exc

    def _wrap(code: ProviderErrorCode, message: str, **kw) -> ProviderError:
        return ProviderError(code, message, provider=provider, model=model,
                             kind=kind, cause=exc, **kw)

    try:  # httpx 是运行时依赖；缺失时退化为标准异常分类
        import httpx
    except ImportError:  # pragma: no cover - httpx 在所有部署形态都存在
        httpx = None

    # 超时：asyncio.TimeoutError 在 3.11+ 就是 TimeoutError
    timeout_types: tuple[type[BaseException], ...] = (TimeoutError,)
    if httpx is not None:
        timeout_types = (TimeoutError, httpx.TimeoutException)
    if isinstance(exc, timeout_types):
        return _wrap(ProviderErrorCode.TIMEOUT, str(exc))

    if httpx is not None and isinstance(exc, httpx.HTTPStatusError):
        status = _status_of(exc)
        if status in (401, 403):
            code = ProviderErrorCode.AUTH_FAILED
        elif status == 402:
            # Payment Required：上游账户余额/配额耗尽
            code = ProviderErrorCode.QUOTA_EXCEEDED
        elif status == 429:
            code = (ProviderErrorCode.QUOTA_EXCEEDED
                    if _looks_like_quota(exc)
                    else ProviderErrorCode.RATE_LIMITED)
        elif status is not None and 500 <= status < 600:
            code = ProviderErrorCode.UNAVAILABLE
        elif status is not None and 400 <= status < 500:
            # 4xx（非 401/403/429）：请求本身被上游拒绝（schema/参数不合规），
            # 重试没有意义，归入 invalid_response
            code = ProviderErrorCode.INVALID_RESPONSE
        else:
            code = ProviderErrorCode.UNAVAILABLE
        hint = ""
        if status == 401:
            hint = " (检查 API Key 是否有效/过期)"
        elif code is ProviderErrorCode.QUOTA_EXCEEDED:
            hint = " (上游账户余额或配额不足：请充值或更换 API Key)"
        return _wrap(code, f"HTTP {status}{hint}: {exc}", status=status)

    if httpx is not None and isinstance(exc, httpx.HTTPError):
        # 连接失败/协议错误等
        return _wrap(ProviderErrorCode.UNAVAILABLE, str(exc))

    # 响应结构不符合预期：JSON 解析失败、缺 choices/data 字段等
    if isinstance(exc, (ValueError, KeyError, IndexError, TypeError)) \
            and _looks_like_response_error(exc):
        return _wrap(ProviderErrorCode.INVALID_RESPONSE, str(exc))

    return None


_RESPONSE_ERROR_HINTS = (
    "json", "choices", "embedding", "content", "message", "vector",
    "维度", "缺少", "响应",
)


def _looks_like_response_error(exc: BaseException) -> bool:
    """粗筛响应解析异常：避免把任意 TypeError（代码 bug）都吞成
    invalid_response。只在消息里出现响应相关关键词时才归类。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(h in text for h in _RESPONSE_ERROR_HINTS)
