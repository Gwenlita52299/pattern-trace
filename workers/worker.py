"""Arq worker settings — 队列分离部署形态（backend-api-spec §3）。

管线本体在 backend.services.orchestration.run_analysis（进程内包 D1）；
worker 只是把同一函数搬到独立进程执行。API 进程内 asyncio.create_task
是默认执行载体（单实例部署无需 Redis 队列），此入口供扩缩容时切换。
"""
from __future__ import annotations

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


class WorkerSettings:
    # arq 直接读取类属性（不实例化），必须在类定义时求值；
    # 放在 __init__ 里永远不会执行，会退化成连 localhost:6379
    functions: ClassVar[list] = [run_analysis]
    redis_settings = get_redis_settings()
    queue_name = os.environ.get("ARQ_QUEUE_GRAPH", "q_graph")
