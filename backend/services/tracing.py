"""分析阶段持久化 trace（issue #73）。

分工（issue 明确要求）：
- Redis（_report_stage）继续承担**实时**阶段通知（前端进度条轮询用）
- 本模块写入 analysis_spans，作为**历史事实源**：Redis TTL 过期、阶段被
  后续阶段覆盖、或分析早已结束，都不影响追溯

每个 span 在进入时插入 status=running 行（因此「进行中」阶段也能被观测，
worker 崩溃则留下 running 行表示中断），退出时更新 finished_at /
duration_ms / status / error_code / metadata。

写入一律 best-effort：观测失败绝不阻塞分析管线（与既有 _record_stage
同级容忍度）。同步与异步上下文管理器由同一个对象实现——图构建跑在线程池、
embedding 是同步 HTTP，而检索/LLM 是异步，两条路径都要能用。

脱敏是硬约束（验收标准）：metadata 只接受白名单之外的**标量计数与版本号**，
键名命中敏感集合（authorization/api_key/cookie/...）或载荷类（prompt/
messages/thinking/response/body/headers）一律丢弃；长字符串截断。本模块
不提供任何写入原始请求/响应载荷的入口。
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from ..core.providers.wrappers import _SENSITIVE_KEYS

# 载荷类键名：即使不是凭据也不能落库（验收：不记录完整 prompt、原始
# 模型思维链、未脱敏响应）
_PAYLOAD_KEYS = frozenset({
    "prompt", "messages", "thinking", "reasoning", "response", "raw",
    "raw_response", "body", "request", "request_body", "payload", "content",
    "text", "input", "output", "headers", "url", "base_url", "traceback",
})
_FORBIDDEN_KEYS = _SENSITIVE_KEYS | _PAYLOAD_KEYS

_MAX_STR = 300          # 单个字符串值上限（防误传长文本）
_MAX_METADATA_BYTES = 8192

_ENGINES: dict = {}


def _engine():
    """按 URL 缓存 engine（trace 写入来自线程池，不能复用请求级 session）。"""
    from ..core.config import get_settings

    url = get_settings().database_url.replace(
        "postgresql://", "postgresql+psycopg://")
    engine = _ENGINES.get(url)
    if engine is None:
        from sqlalchemy import create_engine

        engine = create_engine(url, pool_pre_ping=True)
        _ENGINES[url] = engine
    return engine


def reset_engine() -> None:
    """测试用：配置变更后丢弃缓存的 engine。"""
    _ENGINES.clear()


def _sanitize(value, depth: int = 0):
    """递归脱敏/裁剪 metadata 值；不可序列化类型退化为 repr 截断。"""
    if depth > 4:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else value[:_MAX_STR] + "…"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k)
            if key.lower() in _FORBIDDEN_KEYS:
                out[key] = "[REDACTED]"
            else:
                out[key] = _sanitize(v, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(v, depth + 1) for v in list(value)[:50]]
    return f"<{type(value).__name__}>"


def sanitize_metadata(metadata: dict | None) -> dict | None:
    if not metadata:
        return None
    clean = _sanitize(metadata)
    if not isinstance(clean, dict):
        return None
    if len(json.dumps(clean, default=str)) > _MAX_METADATA_BYTES:
        return {"truncated": True}
    return clean


def _error_code_for(exc: BaseException) -> str:
    """稳定错误码：优先异常的 error_code/code 属性，否则类名（不含消息）。"""
    for attr in ("code", "error_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value:
            return value[:50]
    return type(exc).__name__[:50]


class _Span:
    """span 上下文：`with` 与 `async with` 均可用。"""

    def __init__(self, recorder: "SpanRecorder", name: str, *,
                 attempt: int = 1, metadata: dict | None = None):
        self._recorder = recorder
        self._name = name
        self._attempt = attempt
        self._started_monotonic = 0.0
        self._id: int | None = None
        self._status: str | None = None
        self._error_code: str | None = None
        self.meta: dict = dict(metadata or {})

    def set(self, **fields) -> "_Span":
        """补充 metadata（阶段结束前才知道的计数/版本）。"""
        self.meta.update(fields)
        return self

    def set_status(self, status: str, *, error_code: str | None = None) -> None:
        """显式覆盖退出时的状态（如「重试排期」「取消」等业务结论）。"""
        self._status = status
        if error_code is not None:
            self._error_code = error_code

    def __enter__(self) -> "_Span":
        self._begin()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._end(exc)
        return False

    async def __aenter__(self) -> "_Span":
        self._begin()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._end(exc)
        return False

    def _begin(self) -> None:
        self._started_monotonic = time.perf_counter()
        self._id = self._recorder._insert_running(
            self._name, attempt=self._attempt, parent_id=self._recorder._parent_id)

    def _end(self, exc: BaseException | None) -> None:
        duration_ms = int((time.perf_counter() - self._started_monotonic) * 1000)
        status = self._status or ("completed" if exc is None else "failed")
        error_code = self._error_code or (_error_code_for(exc) if exc else None)
        self._recorder._finish(self._id, status=status, duration_ms=duration_ms,
                               error_code=error_code, metadata=self.meta)


class _NullSpan(_Span):
    """未启用 trace 时的空实现（调用方无需判空）。"""

    def _begin(self) -> None:
        return

    def _end(self, exc) -> None:
        return


class NullRecorder:
    """未启用持久化 trace 的占位（保持调用方代码路径一致）。"""

    enabled = False

    def span(self, name: str, *, attempt: int = 1,
             metadata: dict | None = None) -> _Span:
        return _NullSpan(self, name, attempt=attempt, metadata=metadata)


class SpanRecorder:
    """一次分析的 span 记录器；线程池与事件循环可共用（无共享可变状态）。

    `root=True` 的 span 成为 trace 根（judgment 级），其余 span 自动挂到根
    之下——前端据此把阶段耗时归到一次分析。
    """

    enabled = True

    def __init__(self, judgment_id: str, trace_id: str | None = None):
        self.judgment_id = judgment_id
        self.trace_id = trace_id
        self.root_span_id: int | None = None
        self._pending_root = False

    def span(self, name: str, *, attempt: int = 1, root: bool = False,
             metadata: dict | None = None) -> _Span:
        self._pending_root = root
        return _Span(self, name, attempt=attempt, metadata=metadata)

    # -- 写入 ------------------------------------------------------------
    @property
    def _parent_id(self) -> int | None:
        # root=True 的 span 自己就是根；其余挂到已记录的根
        return None if self._pending_root else self.root_span_id

    def _insert_running(self, name: str, *, attempt: int,
                        parent_id: int | None) -> int | None:
        from ..models.base import AnalysisSpan

        try:
            from sqlalchemy.orm import Session

            with Session(_engine()) as session:
                row = AnalysisSpan(
                    judgment_id=self.judgment_id, trace_id=self.trace_id,
                    parent_id=parent_id, name=name, status="running",
                    attempt=attempt, started_at=datetime.now(UTC))
                session.add(row)
                session.commit()
                span_id = row.id
        except Exception as exc:  # noqa: BLE001 — 观测绝不阻塞分析
            print(f"[tracing] span {name} insert failed ({exc!r})")
            return None
        if self._pending_root:
            self.root_span_id = span_id
            self._pending_root = False
        return span_id

    def _finish(self, span_id: int | None, *, status: str, duration_ms: int,
                error_code: str | None, metadata: dict | None) -> None:
        if span_id is None:
            return
        from ..models.base import AnalysisSpan

        try:
            from sqlalchemy import update
            from sqlalchemy.orm import Session

            with Session(_engine()) as session:
                session.execute(
                    update(AnalysisSpan)
                    .where(AnalysisSpan.id == span_id)
                    .values(status=status, duration_ms=duration_ms,
                            finished_at=datetime.now(UTC),
                            error_code=error_code,
                            span_metadata=sanitize_metadata(metadata)))
                session.commit()
        except Exception as exc:  # noqa: BLE001 — 观测绝不阻塞分析
            print(f"[tracing] span {span_id} finish failed ({exc!r})")


def list_spans(session, judgment_id: str) -> list:
    """按开始时间返回某 judgment 的全部 span（trace 端点用）。"""
    from sqlalchemy import select

    from ..models.base import AnalysisSpan

    return list(session.execute(
        select(AnalysisSpan)
        .where(AnalysisSpan.judgment_id == judgment_id)
        .order_by(AnalysisSpan.started_at, AnalysisSpan.id)).scalars().all())


def span_payload(row) -> dict:
    """span 的对外 JSON 形态（字段名与 issue 描述一致）。"""
    duration_ms = row.duration_ms
    if duration_ms is None and row.started_at is not None:
        end = row.finished_at or datetime.now(UTC)
        duration_ms = int((end - row.started_at).total_seconds() * 1000)
    return {
        "id": row.id,
        "parent_id": row.parent_id,
        "name": row.name,
        "status": row.status,
        "attempt": row.attempt,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "duration_ms": duration_ms,
        "error_code": row.error_code,
        "metadata": row.span_metadata or {},
    }


__all__ = [
    "SpanRecorder", "NullRecorder", "_Span", "sanitize_metadata",
    "list_spans", "span_payload", "reset_engine",
]
