"""Arq worker settings — 队列分离部署形态（issue #22 / #74）。

两个独立 worker 进程消费两个队列：
- AnalysisWorkerSettings → q_analysis（交互式分析，max_jobs 可配较大）
- ReportWorkerSettings   → q_report（PDF/HTML 渲染，max_jobs 默认 1）
物理隔离保证报告堆积不会挤占分析槽位（issue #74 验收 #2）。

任务壳层的职责（issue #74）：
- orchestration/report_service 内部已把「业务失败」写成终态并返回状态；
  壳层负责**任务级重试与死信**：瞬时错误（provider 超时/限流/上游不可用）
  退避重试，永久错误或重试耗尽落死信并同步业务失败状态。
- 重试使用 arq 原生 `Retry(defer=...)`：任务留在队列（job_try 递增），
  worker 重启后仍能继续，不需要额外状态。
"""
from __future__ import annotations

import asyncio
import os
from typing import ClassVar

from arq import Retry
from arq.connections import RedisSettings

from backend.services.task_governance import (archive_failure_sync,
                                              business_failure_sync,
                                              is_transient)


def get_redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    )


def _settings():
    from backend.core.config import get_settings

    return get_settings()


def _retry_delay(attempt: int) -> float:
    """指数退避（attempt 从 1 起）：base * 2^(n-1)，封顶 max_delay。"""
    s = _settings()
    base = float(getattr(s, "task_retry_base_delay", 5.0))
    cap = float(getattr(s, "task_retry_max_delay", 30.0))
    return min(base * (2 ** max(attempt - 1, 0)), cap)


def _task_max_tries() -> int:
    return int(getattr(_settings(), "task_max_tries", 3))


async def _finish_or_retry(ctx, *, task_type: str, business_id: str,
                           queue: str, attempt: int,
                           error_code: str | None, message: str) -> dict:
    """瞬时错误且未耗尽 → 退避重试；否则落死信（业务终态已由执行侧写）。"""
    max_tries = _task_max_tries()
    if is_transient(error_code) and attempt < max_tries:
        delay = _retry_delay(attempt)
        print(f"[worker] {task_type} {business_id} transient "
              f"({error_code}); retry {attempt + 1}/{max_tries} in {delay:.0f}s")
        raise Retry(defer=delay)
    await asyncio.to_thread(
        archive_failure_sync, task_type=task_type, business_id=business_id,
        queue=queue, attempts=attempt, error_code=error_code,
        message=message or f"{task_type} failed")
    print(f"[worker] {task_type} {business_id} dead-lettered "
          f"after {attempt} attempt(s): {error_code}")
    key = "judgment_id" if task_type == "analysis" else "report_id"
    return {key: business_id, "status": "failed", "error_code": error_code}


async def run_analysis(ctx, judgment_id: str) -> dict:
    from backend.services.orchestration import run_analysis as _run

    attempt = int((ctx or {}).get("job_try", 1) or 1)
    try:
        status = await _run(judgment_id, attempt=attempt)
    except Exception as exc:  # noqa: BLE001 — 壳层兜底：分类后重试或死信
        code = getattr(exc, "code", None) or type(exc).__name__
        return await _finish_or_retry(
            ctx, task_type="analysis", business_id=judgment_id,
            queue=analysis_queue_name(), attempt=attempt,
            error_code=str(code), message=repr(exc))
    if status == "retrying":
        # 执行侧已把业务行回落 queued；这里只负责排期下一次尝试
        delay = _retry_delay(attempt)
        print(f"[worker] analysis {judgment_id} retry "
              f"{attempt + 1}/{_task_max_tries()} in {delay:.0f}s")
        raise Retry(defer=delay)
    if status == "failed":
        error_code, message = await asyncio.to_thread(
            business_failure_sync, "analysis", judgment_id)
        return await _finish_or_retry(
            ctx, task_type="analysis", business_id=judgment_id,
            queue=analysis_queue_name(), attempt=attempt,
            error_code=error_code, message=message)
    return {"judgment_id": judgment_id, "status": status}


async def run_report(ctx, report_id: str) -> dict:
    from backend.services.report_service import generate_report

    attempt = int((ctx or {}).get("job_try", 1) or 1)
    try:
        status = await asyncio.to_thread(generate_report, report_id)
    except Exception as exc:  # noqa: BLE001 — 壳层兜底
        code = getattr(exc, "code", None) or type(exc).__name__
        return await _finish_or_retry(
            ctx, task_type="report", business_id=report_id,
            queue=report_queue_name(), attempt=attempt,
            error_code=str(code), message=repr(exc))
    if status == "failed":
        error_code, message = await asyncio.to_thread(
            business_failure_sync, "report", report_id)
        return await _finish_or_retry(
            ctx, task_type="report", business_id=report_id,
            queue=report_queue_name(), attempt=attempt,
            error_code=error_code, message=message)
    return {"report_id": report_id, "status": status}


def analysis_queue_name() -> str:
    from backend.services.task_queue import analysis_queue_name as _name

    return _name()


def report_queue_name() -> str:
    from backend.services.task_queue import report_queue_name as _name

    return _name()


def collect_stuck(session) -> tuple[list[str], list[str]]:
    """worker 重启恢复的事实源查询（issue #22 / #74）。

    - Judgment 卡 queued：API 可能在「落库后、enqueue 前」崩溃，或任务消息
      已从 Redis 丢失 → 需重新投递。
    - Report 卡 queued（issue #74）：已受理但未执行（投递丢失/worker 重启）
      → 重新投递；卡 processing：worker 中途崩溃后该行永远占用 BE-27 并发
      配额 → 同样重新投递。

    Judgment 卡 processing 不在此列：可能是另一 worker 正在执行，且 run_analysis
    的 claim 只从 queued 抢占（重放消息撞上 processing 直接 skipped），卡死行
    交给 reclaim_zombies 判 TASK_TIMEOUT 终态。
    """
    from sqlalchemy import select

    from backend.models.base import Judgment, Report

    queued_ids = session.execute(
        select(Judgment.id).where(Judgment.status == "queued")
    ).scalars().all()
    report_ids = session.execute(
        select(Report.id).where(Report.status.in_(("queued", "processing")))
    ).scalars().all()
    return list(queued_ids), list(report_ids)


async def recover_stuck_tasks(ctx) -> None:
    """worker 启动钩子：把 DB 里丢失投递的任务重新 enqueue。

    重放安全性：run_analysis 有终态守卫 + claim 抢占（并发竞争下只有一方
    执行）；generate_report 对 queued/processing 行幂等（claim + 乐观守卫 +
    同 storage_key 覆盖写）。两个 worker 都会跑本钩子，重复投递由确定性
    job_id 去重。
    """
    from sqlalchemy.orm import Session

    from backend.api.app import close_db_engine, get_db_engine

    engine = get_db_engine()
    with Session(engine) as session:
        queued_ids, report_ids = collect_stuck(session)
    # ...enqueue 后统一释放连接池
    close_db_engine()
    redis = ctx.get("redis")
    if redis is None:  # arq 正常总会注入；缺守卫时 AttributeError 会令 worker 启动崩循环
        print("[worker] recover: no redis in ctx; skip")
        return
    # 确定性 _job_id：原消息仍在队列时 arq 按 job key 去重跳过，避免双投递
    for jid in queued_ids:
        print(f"[worker] recover: re-enqueue judgment {jid}")
        await redis.enqueue_job("run_analysis", jid,
                                _job_id=f"run_analysis:{jid}",
                                _queue_name=analysis_queue_name())
    for rid in report_ids:
        print(f"[worker] recover: re-enqueue report {rid}")
        await redis.enqueue_job("run_report", rid,
                                _job_id=f"run_report:{rid}",
                                _queue_name=report_queue_name())
    close_db_engine()


class AnalysisWorkerSettings:
    # arq 直接读取类属性（不实例化），必须在类定义时求值；
    # 放在 __init__ 里永远不会执行，会退化成连 localhost:6379
    functions: ClassVar[list] = [run_analysis]
    redis_settings = get_redis_settings()
    queue_name = os.environ.get("ARQ_QUEUE_ANALYSIS") \
        or os.environ.get("ARQ_QUEUE_GRAPH", "q_analysis")
    on_startup = recover_stuck_tasks
    max_jobs = int(os.environ.get("ARQ_MAX_JOBS", "4"))
    # 分析管线含 LLM 调用，可能远超默认 300s；worker 崩溃时任务由
    # DB 侧 reclaim_zombies 兜底进终态，这里的超时只是单任务硬上限。
    # 注意：arq 只识别 WorkerSettings 里与 Worker.__init__ 同名的属性，
    # 参数名是 job_timeout（写 timeout 会被静默忽略回退到默认 300s）
    job_timeout = int(os.environ.get("ARQ_JOB_TIMEOUT", "900"))


class ReportWorkerSettings:
    functions: ClassVar[list] = [run_report]
    redis_settings = get_redis_settings()
    queue_name = os.environ.get("ARQ_QUEUE_REPORT", "q_report")
    on_startup = recover_stuck_tasks
    # 报告渲染是 CPU 密集（PDF 生成），默认单并发避免拖慢分析 worker
    max_jobs = int(os.environ.get("ARQ_REPORT_MAX_JOBS", "1"))
    job_timeout = int(os.environ.get("ARQ_REPORT_JOB_TIMEOUT", "600"))


# 兼容旧入口：arq workers.worker.WorkerSettings 与既有 compose/测试引用
WorkerSettings = AnalysisWorkerSettings
