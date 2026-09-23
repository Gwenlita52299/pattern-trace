"""任务治理（issue #74）：瞬时/永久错误分类、死信、队列状态、取消与重跑。

为什么要独立一层：队列投递（task_queue）与任务执行（worker / orchestration）
都不该承担治理职责——死信记录、取消语义、队列深度观测是横切关注点，
放在这里让 worker 壳层只需调一个函数。

瞬时 vs 永久（决定重试与否）：
- 瞬时（可重试）：provider 超时/限流/上游不可用、Esplora 完全不可用——
  外部系统抖动，退避后可能成功
- 永久（不重试）：校验失败、认证失败、配置错误、请求畸形、未知异常——
  重试只是浪费配额并延迟终态（认证失败重试一万次也不会变好）
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

__all__ = [
    "TRANSIENT_ERROR_CODES", "is_transient", "record_dead_letter",
    "list_dead_letters", "prepare_requeue", "mark_requeued",
    "collect_queue_status", "cancel_task", "CANCELLABLE_STATUSES",
    "archive_failure_sync", "business_failure_sync",
]

# 可重试的错误码（与 provider 统一分类、Esplora 建图错误码对齐）
TRANSIENT_ERROR_CODES = frozenset({
    "LLM_PROVIDER_TIMEOUT",
    "LLM_PROVIDER_RATE_LIMITED",
    "EMBEDDING_TIMEOUT",
    "EMBEDDING_RATE_LIMITED",
    "EMBEDDING_ERROR",
    "ESPLORA_UNAVAILABLE",
    "TASK_TIMEOUT",
})

# 排队中/执行中才可取消；终态不可再改（状态机不可变）
CANCELLABLE_STATUSES = frozenset({"queued", "processing"})


def is_transient(error_code: str | None) -> bool:
    return bool(error_code) and error_code in TRANSIENT_ERROR_CODES


def record_dead_letter(session, *, task_type: str, business_id: str,
                       queue: str, attempts: int, error_code: str | None,
                       message: str) -> "object":  # noqa: F821 - 返回 ORM 行
    """重试耗尽/永久错误时归档任务（同一业务 ID 只保留最新一条）。"""
    from backend.models.base import TaskDeadLetter

    existing = session.execute(
        select(TaskDeadLetter).where(
            TaskDeadLetter.task_type == task_type,
            TaskDeadLetter.business_id == business_id,
            TaskDeadLetter.requeued_at.is_(None))
    ).scalars().first()
    if existing is not None:
        # 同一次失败链的重复归档（并发重试壳层）只更新最新错误与次数
        existing.attempts = max(existing.attempts, attempts)
        existing.error_code = error_code
        existing.last_error = (message or "")[:2000]
        existing.failed_at = datetime.now(UTC)
        row = existing
    else:
        row = TaskDeadLetter(
            task_type=task_type, business_id=business_id, queue=queue,
            attempts=attempts, error_code=error_code,
            last_error=(message or "")[:2000])
        session.add(row)
    session.commit()
    return row


def business_failure_sync(task_type: str, business_id: str) -> tuple[
        str | None, str]:
    """同步读业务对象的失败码与消息（死信归档与壳层共用）。

    worker 与降级路径都在同步上下文里归档，共用一份读实现避免漂移。
    """
    from sqlalchemy.orm import Session

    from backend.api.app import close_db_engine, get_db_engine
    from backend.models.base import Judgment, Report

    engine = get_db_engine()
    try:
        with Session(engine) as session:
            if task_type == "pattern_index":
                # issue #75：索引失败码由 revision 行承载（embedding 故障等）
                from backend.models.knowledge import PatternRevision

                try:
                    rev = session.get(PatternRevision, int(business_id))
                except (TypeError, ValueError):
                    rev = None
                if rev is None:
                    return None, "revision not found"
                code = ("PATTERN_INDEX_FAILED"
                        if rev.index_status == "failed" else None)
                return code, (rev.index_error or "")
            model = Judgment if task_type == "analysis" else Report
            row = session.get(model, business_id)
            if row is None:
                return None, "business object not found"
            return row.error_code, (getattr(row, "error_message", "") or "")
    finally:
        close_db_engine()


def archive_failure_sync(*, task_type: str, business_id: str, queue: str,
                         attempts: int, error_code: str | None,
                         message: str) -> None:
    """同步归档死信（worker 壳层与进程内降级路径共用）。"""
    from sqlalchemy.orm import Session

    from backend.api.app import close_db_engine, get_db_engine

    engine = get_db_engine()
    try:
        with Session(engine) as session:
            record_dead_letter(session, task_type=task_type,
                               business_id=business_id, queue=queue,
                               attempts=attempts, error_code=error_code,
                               message=message)
    finally:
        close_db_engine()


def list_dead_letters(session, *, page: int = 1, page_size: int = 20,
                      task_type: str | None = None,
                      unresolved_only: bool = True) -> tuple[list, int]:
    """死信分页查询（默认只看未重跑的）。"""
    from backend.models.base import TaskDeadLetter

    conds = []
    if task_type:
        conds.append(TaskDeadLetter.task_type == task_type)
    if unresolved_only:
        conds.append(TaskDeadLetter.requeued_at.is_(None))
    total = session.execute(
        select(func.count(TaskDeadLetter.id)).where(*conds)).scalar_one()
    rows = session.execute(
        select(TaskDeadLetter).where(*conds)
        .order_by(TaskDeadLetter.failed_at.desc(),
                  TaskDeadLetter.id.desc())
        .offset((page - 1) * page_size).limit(page_size)).scalars().all()
    return list(rows), int(total)


def _business_row(session, task_type: str, business_id: str):
    from backend.models.base import Judgment, Report

    model = Judgment if task_type == "analysis" else Report
    return session.get(model, business_id)


def prepare_requeue(session, dead_letter, *, actor: str) -> int:
    """把失败的业务对象重置为 queued，返回新的 attempt 序号（0=拒绝）。

    仅当业务对象确实处于 failed 终态时允许——已 completed 或被人工取消的
    任务不应被死信重跑悄悄复活（状态机不可变的延长线）。
    """
    row = _business_row(session, dead_letter.task_type,
                        dead_letter.business_id)
    if row is None or row.status != "failed":
        return 0
    row.status = "queued"
    row.error_code = None
    if hasattr(row, "error_message"):
        row.error_message = None
    dead_letter.requeued_at = datetime.now(UTC)
    dead_letter.requeued_by = actor
    session.commit()
    return int(dead_letter.attempts) + 1


def mark_requeued(session, dead_letter, *, actor: str) -> None:
    dead_letter.requeued_at = datetime.now(UTC)
    dead_letter.requeued_by = actor
    session.commit()


def cancel_task(session, *, task_type: str, business_id: str,
                actor: str) -> str:
    """取消任务：排队/执行中的任务置 cancelled（终态）。

    排队任务取消后 worker 执行时会发现状态已变而跳过；执行中任务在
    orchestration 的阶段边界检查到 cancelled 后协作式退出（不抢占线程）。
    返回 cancelled / not_found / not_cancellable。
    """
    from backend.models.base import JudgmentEvent

    row = _business_row(session, task_type, business_id)
    if row is None:
        return "not_found"
    if row.status not in CANCELLABLE_STATUSES:
        return "not_cancellable"
    previous = row.status
    row.status = "cancelled"
    row.error_code = "CANCELLED"
    if hasattr(row, "concluded_at"):
        row.concluded_at = datetime.now(UTC)
    if task_type == "analysis":
        session.add(JudgmentEvent(
            judgment_id=business_id, from_status=previous,
            to_status="cancelled",
            detail={"cancelled_by": actor}))
    session.commit()
    return "cancelled"


async def _queued_jobs(queue: str) -> list:
    """读队列中的待执行任务（arq 公开 API，不依赖内部 key 约定）。

    复用 task_queue 的进程级连接池；Redis 不可用时抛异常由调用方降级。
    """
    from backend.services import task_queue

    if task_queue._pool is None:  # noqa: SLF001 - 治理层复用投递层连接
        from arq import create_pool

        from workers.worker import get_redis_settings

        settings = get_redis_settings()
        settings.conn_retries = 1
        task_queue._pool = await create_pool(  # noqa: SLF001
            settings, default_queue_name=queue)
    return await task_queue._pool.queued_jobs(queue_name=queue)  # noqa: SLF001


async def collect_queue_status(session) -> dict:
    """队列状态：pending/active/retry + 最老等待时长 + 死信计数。

    pending/retry 来自队列（arq queued_jobs 公开 API，job_try>1 即重试中），
    active 来自 DB 的 processing 计数（业务语义，不依赖 arq 内部 key）。
    Redis 不可用时返回 redis_available=false 而非报错——治理接口本身
    不应因为被观测对象故障而不可用。
    """
    from backend.models.base import Judgment, Report, TaskDeadLetter
    from backend.services.task_queue import (analysis_queue_name,
                                             index_queue_name,
                                             report_queue_name)

    now = datetime.now(UTC)
    out: dict = {"redis_available": True, "queues": {}}
    for label, queue in (("analysis", analysis_queue_name()),
                         ("report", report_queue_name()),
                         ("index", index_queue_name())):
        info = {"queue": queue, "pending": 0, "retry": 0,
                "oldest_waiting_seconds": None}
        try:
            jobs = await _queued_jobs(queue)
        except Exception as exc:  # noqa: BLE001 - 观测降级，不抛给调用方
            out["redis_available"] = False
            info["error"] = f"{type(exc).__name__}"
            out["queues"][label] = info
            continue
        info["pending"] = len(jobs)
        info["retry"] = sum(1 for j in jobs
                            if int(getattr(j, "job_try", 1) or 1) > 1)
        times = [j.enqueue_time for j in jobs
                 if getattr(j, "enqueue_time", None) is not None]
        if times:
            enqueue_time = min(times)
            if enqueue_time.tzinfo is None:
                enqueue_time = enqueue_time.replace(tzinfo=UTC)
            info["oldest_waiting_seconds"] = round(
                (now - enqueue_time).total_seconds(), 1)
        out["queues"][label] = info

    active_map = {"analysis": (Judgment, "analysis"),
                  "report": (Report, "report")}
    for label, (model, _t) in active_map.items():
        out["queues"][label]["active"] = int(session.execute(
            select(func.count(model.id))
            .where(model.status == "processing")).scalar_one())
    # issue #75：索引重算的「进行中」以 PatternRevision 的 pending 计数表达
    # （revision 行没有 processing 态，pending 即待重算/重算中）
    from backend.models.knowledge import PatternRevision

    out["queues"]["index"]["active"] = int(session.execute(
        select(func.count(PatternRevision.id))
        .where(PatternRevision.index_status == "pending")).scalar_one())

    total = int(session.execute(
        select(func.count(TaskDeadLetter.id))).scalar_one())
    unresolved = int(session.execute(
        select(func.count(TaskDeadLetter.id))
        .where(TaskDeadLetter.requeued_at.is_(None))).scalar_one())
    out["dead_letters"] = {"total": total, "unresolved": unresolved}
    return out
