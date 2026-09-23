"""任务投递层（issue #22 / #74）：分析/报告分队列经 Arq 持久化投递。

Redis 可达时 enqueue_job 进持久化队列——API 重启不丢任务，worker 消费；
不可达时的行为由 `QUEUE_REQUIRED` 决定（issue #74）：
- false（开发默认）：降级为进程内 asyncio 任务，保留单实例无 Redis 的
  合法部署形态（backend-api-spec §3），代价是该形态不承诺重启恢复
- true（生产）：直接抛 QueueUnavailable → API 返回 503，**不得静默降级**
  （静默降级在重启后会丢任务，运维却以为任务已排队）

队列拆分：分析与报告使用独立队列（q_analysis / q_report），由独立 worker
进程消费并各自设置并发——报告渲染可能长时间占用槽位，不得挤占交互式分析。
是否走队列以「投递动作是否成功」为准，不预先 ping（避免额外探测往返）。
"""
from __future__ import annotations

import asyncio
import os
import time

_pool = None  # 进程生命周期内复用；失败置 None，下次投递重试建连
_fail_until = 0.0  # 建连失败后的冷却期：期间直接降级，避免每请求阻塞重试
_COOLDOWN_SECONDS = 30


class QueueUnavailable(RuntimeError):
    """Redis 不可用且 QUEUE_REQUIRED=true：拒绝投递（issue #74）。

    生产环境静默降级为进程内任务会在 API 重启后丢任务，且没有任何信号；
    显式 503 让调用方/运维知道任务未入队。
    """

    code = "QUEUE_UNAVAILABLE"


def _queue_from_env(primary: str, legacy: str, fallback: str) -> str:
    return (os.environ.get(primary) or os.environ.get(legacy)
            or fallback)


def analysis_queue_name() -> str:
    # ARQ_QUEUE_GRAPH 是拆分前的旧队列名，保留回退以兼容既有部署
    return _queue_from_env("ARQ_QUEUE_ANALYSIS", "ARQ_QUEUE_GRAPH",
                           "q_analysis")


def report_queue_name() -> str:
    return _queue_from_env("ARQ_QUEUE_REPORT", "", "q_report")


def index_queue_name() -> str:
    # issue #75：模式索引重算独立队列——embedding 网络调用 + graphormer
    # 前向会长时间占用槽位，不得挤占交互式分析或报告渲染
    return _queue_from_env("ARQ_QUEUE_INDEX", "", "q_index")


def queue_name() -> str:
    """（兼容保留）默认队列名 = 分析队列。"""
    return analysis_queue_name()


def _queue_required() -> bool:
    # 惰性 import：本模块被子进程/测试直接引用，避免循环依赖
    from backend.core.config import get_settings

    return bool(getattr(get_settings(), "queue_required", False))


async def _enqueue(job_name: str, *args, job_id: str,
                   queue: str | None = None,
                   defer_by: float | None = None) -> bool:
    global _pool, _fail_until
    if time.monotonic() < _fail_until:
        if _queue_required():
            raise QueueUnavailable(
                f"queue unavailable (cooling down), {job_name} not enqueued")
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
                settings, default_queue_name=queue or analysis_queue_name())
        kwargs: dict = {"_job_id": job_id, "_queue_name": queue}
        if defer_by is not None:
            kwargs["_defer_by"] = defer_by
        await _pool.enqueue_job(job_name, *args, **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001 — Redis 不可达是合法降级路径
        _pool = None
        _fail_until = time.monotonic() + _COOLDOWN_SECONDS
        if _queue_required():
            raise QueueUnavailable(
                f"queue unavailable ({exc!r}); {job_name} not enqueued; "
                "QUEUE_REQUIRED=true") from exc
        print(f"[task_queue] enqueue {job_name} failed ({exc!r}); "
              "falling back to in-process task")
        return False


_bg_tasks: set[asyncio.Task] = set()  # 降级路径：强引用防 Task 被 GC


def _spawn(coro) -> None:
    """进程内降级任务必须跑在**进程级**后台 loop 上。

    issue #78：TestClient 的 portal 是 per-request 的——task 绑定到
    发起请求的 event loop 后，该请求返回即 portal 关闭，协程被冻结
    （judgment 永远 queued/processing，无任何报错）。所以 spawn 一律
    投递到独立的 daemon 后台 loop（run_coroutine_threadsafe），与请求
    生命周期解耦；uvicorn 真实形态下同一机制也保证任务不受单请求
    生命周期影响。
    """
    try:
        loop = _bg_loop()
        _ = asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception as exc:  # noqa: BLE001 — 降级路径的失败只告警
        print(f"[task_queue] in-process dispatch failed ({exc!r})")


_bg_loop_handle = None  # (thread, loop)


def _bg_loop():
    """单例后台 event loop（daemon 线程），进程生命周期内复用。"""
    global _bg_loop_handle
    if _bg_loop_handle is None:
        import threading

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True,
                                  name="task-queue-inprocess")
        thread.start()
        _bg_loop_handle = (thread, loop)
    return _bg_loop_handle[1]


def _job_id(task: str, business_id: str, attempt: int = 1) -> str:
    """确定性 job id（issue #22 幂等）。重跑（attempt>1）加后缀绕过去重：
    arq 的 job key 在结果保留期内仍在，同 id 会被静默跳过。"""
    base = f"{task}:{business_id}"
    return base if attempt <= 1 else f"{base}:{attempt}"


def _retry_delay(attempt: int) -> float:
    from backend.core.config import get_settings

    s = get_settings()
    base = float(getattr(s, "task_retry_base_delay", 5.0))
    cap = float(getattr(s, "task_retry_max_delay", 30.0))
    return min(base * (2 ** max(attempt - 1, 0)), cap)


async def _run_inprocess_analysis(judgment_id: str) -> None:
    """降级路径（无队列/无 worker）自行完成重试与死信。

    issue #74：回落 queued 的重试语义依赖「有任务壳层重新排期」——在没有
    worker 的单实例部署形态下，这个角色由本次进程内调用来扮演，否则任务
    会停在 queued 直到僵尸回收（120s）才以 TASK_TIMEOUT 收场。
    """
    from .orchestration import run_analysis
    from .task_governance import archive_failure_sync, business_failure_sync

    from backend.core.config import get_settings

    max_tries = int(getattr(get_settings(), "task_max_tries", 3))
    attempt = 1
    while True:
        status = await run_analysis(judgment_id, attempt=attempt)
        if status != "retrying":
            if status == "failed":
                error_code, message = business_failure_sync(
                    "analysis", judgment_id)
                await asyncio.to_thread(
                    archive_failure_sync, task_type="analysis",
                    business_id=judgment_id,
                    queue=analysis_queue_name(), attempts=attempt,
                    error_code=error_code, message=message)
            return
        if attempt >= max_tries:
            return
        await asyncio.sleep(_retry_delay(attempt))
        attempt += 1


async def _run_inprocess_report(report_id: str) -> None:
    from .report_service import generate_report
    from .task_governance import archive_failure_sync, business_failure_sync

    status = await asyncio.to_thread(generate_report, report_id)
    if status == "failed":
        error_code, message = business_failure_sync("report", report_id)
        await asyncio.to_thread(
            archive_failure_sync, task_type="report", business_id=report_id,
            queue=report_queue_name(), attempts=1,
            error_code=error_code, message=message)


async def dispatch_analysis(judgment_id: str, *, attempt: int = 1) -> bool:
    """投递分析任务；返回 True 表示已进持久化队列。

    幂等由两层保证：确定性 job_id（同 attempt 重复投递被 arq 去重）
    + 执行侧 DB 乐观 claim（并发也只允许一方从 queued 抢占成功）。
    """
    if await _enqueue("run_analysis", judgment_id,
                      job_id=_job_id("run_analysis", judgment_id, attempt),
                      queue=analysis_queue_name()):
        return True

    _spawn(_run_inprocess_analysis(judgment_id))
    return False


async def dispatch_report(report_id: str, *, attempt: int = 1) -> bool:
    """投递报告生成任务；返回 True 表示已进持久化队列。"""
    if await _enqueue("run_report", report_id,
                      job_id=_job_id("run_report", report_id, attempt),
                      queue=report_queue_name()):
        return True

    _spawn(_run_inprocess_report(report_id))
    return False


async def _run_inprocess_index(revision_id: int) -> None:
    """降级路径：进程内完成模式索引重算与死信归档（issue #75）。"""
    from .pattern_lifecycle import run_index
    from .task_governance import (archive_failure_sync, business_failure_sync,
                                  is_transient)

    from backend.core.config import get_settings

    max_tries = int(getattr(get_settings(), "task_max_tries", 3))
    attempt = 1
    while True:
        try:
            # 索引重算含 embedding 网络调用与 graphormer 前向，隔离到线程
            await asyncio.to_thread(run_index, revision_id)
            return
        except Exception as exc:  # noqa: BLE001 — 失败已由 run_index 留痕
            error_code, message = await asyncio.to_thread(
                business_failure_sync, "pattern_index", str(revision_id))
            if is_transient(error_code) and attempt < max_tries:
                await asyncio.sleep(_retry_delay(attempt))
                attempt += 1
                continue
            await asyncio.to_thread(
                archive_failure_sync, task_type="pattern_index",
                business_id=str(revision_id), queue=index_queue_name(),
                attempts=attempt, error_code=error_code or "PATTERN_INDEX_FAILED",
                message=message or repr(exc))
            return


async def dispatch_index(revision_id: int, *, attempt: int = 1) -> bool:
    """投递模式索引重算任务；返回 True 表示已进持久化队列。"""
    if await _enqueue("run_index", revision_id,
                      job_id=_job_id("run_index", str(revision_id), attempt),
                      queue=index_queue_name()):
        return True

    _spawn(_run_inprocess_index(revision_id))
    return False
