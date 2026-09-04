"""Arq worker settings — 队列分离部署形态（backend-api-spec §3）。

分析/报告任务由 API 经 backend.services.task_queue 投递进来（issue #22）；
本 worker 是持久化队列的消费端，重启后 Redis 中的排队任务自动继续执行。
"""
from __future__ import annotations

import asyncio
import os
from typing import ClassVar

from arq.connections import RedisSettings


def get_redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    )


async def run_analysis(ctx, judgment_id: str) -> dict:
    from backend.services.orchestration import run_analysis as _run

    status = await _run(judgment_id)
    return {"judgment_id": judgment_id, "status": status}


async def run_report(ctx, report_id: str) -> dict:
    from backend.services.report_service import generate_report

    status = await asyncio.to_thread(generate_report, report_id)
    return {"report_id": report_id, "status": status}


def collect_stuck(session) -> tuple[list[str], list[str]]:
    """worker 重启恢复的事实源查询（issue #22）。

    - Judgment 卡 queued：API 可能在「落库后、enqueue 前」崩溃，或任务消息
      已从 Redis 丢失 → 需重新投递。
    - Report 卡 processing：worker 中途崩溃后该行永远占用 BE-27 并发配额
      → 需重新投递。

    Judgment 卡 processing 不在此列：可能是另一 worker 正在执行，重放会
    双跑（claim 抢占只挡 queued），交给 reclaim_zombies 判 TASK_TIMEOUT 终态。
    """
    from sqlalchemy import select

    from backend.models.base import Judgment, Report

    queued_ids = session.execute(
        select(Judgment.id).where(Judgment.status == "queued")
    ).scalars().all()
    report_ids = session.execute(
        select(Report.id).where(Report.status == "processing")
    ).scalars().all()
    return list(queued_ids), list(report_ids)


async def recover_stuck_tasks(ctx) -> None:
    """worker 启动钩子：把 DB 里丢失投递的任务重新 enqueue。

    重放安全性：run_analysis 有终态守卫 + claim 抢占（并发竞争下只有一方
    执行）；generate_report 对 processing 行幂等（同 storage_key 覆盖写）。
    """
    from sqlalchemy.orm import Session

    from backend.api.app import get_db_engine

    engine = get_db_engine()
    with Session(engine) as session:
        queued_ids, report_ids = collect_stuck(session)
    redis = ctx.get("redis")
    for jid in queued_ids:
        print(f"[worker] recover: re-enqueue judgment {jid}")
        await redis.enqueue_job("run_analysis", jid)
    for rid in report_ids:
        print(f"[worker] recover: re-enqueue report {rid}")
        await redis.enqueue_job("run_report", rid)
    engine.dispose()


class WorkerSettings:
    # arq 直接读取类属性（不实例化），必须在类定义时求值；
    # 放在 __init__ 里永远不会执行，会退化成连 localhost:6379
    functions: ClassVar[list] = [run_analysis, run_report]
    redis_settings = get_redis_settings()
    queue_name = os.environ.get("ARQ_QUEUE_GRAPH", "q_graph")
    on_startup = recover_stuck_tasks
    # 分析管线含 LLM 调用，可能远超默认 300s；worker 崩溃时任务由
    # DB 侧 reclaim_zombies 兜底进终态，这里的超时只是单任务硬上限
    timeout = int(os.environ.get("ARQ_JOB_TIMEOUT", "900"))
