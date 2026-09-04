"""任务投递层（issue #22）：分析/报告统一经 Arq 持久化队列投递给 worker。

Redis 可达时 enqueue_job 进持久化队列——API 重启不丢任务，worker 消费；
不可达时降级为进程内 asyncio 任务，保留单实例无 Redis 的合法部署形态
（backend-api-spec §3），代价是该形态不承诺重启恢复。是否走队列以
「投递动作是否成功」为准，不预先 ping——避免额外探测往返。
"""
from __future__ import annotations

import asyncio
import os

_pool = None  # 进程生命周期内复用；失败置 None，下次投递重试建连


def queue_name() -> str:
    # worker（workers/worker.py）与 API 两侧必须用同一队列名，否则任务互不可见
    return os.environ.get("ARQ_QUEUE_GRAPH", "q_graph")


async def _enqueue(job_name: str, *args) -> bool:
    global _pool
    try:
        if _pool is None:
            from arq import create_pool

            from workers.worker import get_redis_settings

            _pool = await create_pool(
                get_redis_settings(),
                default_queue_name=queue_name())
        await _pool.enqueue_job(job_name, *args)
        return True
    except Exception as exc:  # noqa: BLE001 — Redis 不可达是合法降级路径
        _pool = None
        print(f"[task_queue] enqueue {job_name} failed ({exc!r}); "
              "falling back to in-process task")
        return False


_bg_tasks: set[asyncio.Task] = set()  # 降级路径：强引用防 Task 被 GC


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def dispatch_analysis(judgment_id: str) -> bool:
    """投递分析任务；返回 True 表示已进持久化队列。"""
    if await _enqueue("run_analysis", judgment_id):
        return True
    from .orchestration import run_analysis

    _spawn(run_analysis(judgment_id))
    return False


async def dispatch_report(report_id: str) -> bool:
    """投递报告生成任务；返回 True 表示已进持久化队列。"""
    if await _enqueue("run_report", report_id):
        return True
    from .report_service import generate_report

    _spawn(asyncio.to_thread(generate_report, report_id))
    return False
