"""任务投递层（issue #22）：分析/报告统一经 Arq 持久化队列投递给 worker。

Redis 可达时 enqueue_job 进持久化队列——API 重启不丢任务，worker 消费；
不可达时降级为进程内 asyncio 任务，保留单实例无 Redis 的合法部署形态
（backend-api-spec §3），代价是该形态不承诺重启恢复。是否走队列以
「投递动作是否成功」为准，不预先 ping——避免额外探测往返。
"""
from __future__ import annotations

import asyncio
import os
import time

_pool = None  # 进程生命周期内复用；失败置 None，下次投递重试建连
_fail_until = 0.0  # 建连失败后的冷却期：期间直接降级，避免每请求阻塞重试
_COOLDOWN_SECONDS = 30


def queue_name() -> str:
    # worker（workers/worker.py）与 API 两侧必须用同一队列名，否则任务互不可见
    return os.environ.get("ARQ_QUEUE_GRAPH", "q_graph")


async def _enqueue(job_name: str, *args, job_id: str) -> bool:
    global _pool, _fail_until
    if time.monotonic() < _fail_until:
        return False
    try:
        if _pool is None:
            from arq import create_pool

            from workers.worker import get_redis_settings

            settings = get_redis_settings()
            # arq 默认重试 5 次（~10s 才放弃）；建连失败要快速落降级路径，
            # 由冷却期负责避免高频重试
            settings.conn_retries = 1
            _pool = await create_pool(
                settings, default_queue_name=queue_name())
        await _pool.enqueue_job(job_name, *args, _job_id=job_id)
        return True
    except Exception as exc:  # noqa: BLE001 — Redis 不可达是合法降级路径
        _pool = None
        _fail_until = time.monotonic() + _COOLDOWN_SECONDS
        print(f"[task_queue] enqueue {job_name} failed ({exc!r}); "
              "falling back to in-process task")
        return False


_bg_tasks: set[asyncio.Task] = set()  # 降级路径：强引用防 Task 被 GC


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def dispatch_analysis(judgment_id: str) -> bool:
    """投递分析任务；返回 True 表示已进持久化队列。

    确定性 job_id：幂等重试/API 重发同 id 消息时 arq 按 job key 去重。
    """
    if await _enqueue("run_analysis", judgment_id,
                      job_id=f"run_analysis:{judgment_id}"):
        return True
    from .orchestration import run_analysis

    _spawn(run_analysis(judgment_id))
    return False


async def dispatch_report(report_id: str) -> bool:
    """投递报告生成任务；返回 True 表示已进持久化队列。"""
    if await _enqueue("run_report", report_id,
                      job_id=f"run_report:{report_id}"):
        return True
    from .report_service import generate_report

    _spawn(asyncio.to_thread(generate_report, report_id))
    return False
