"""判断编排：graph-builder → retrieval → llm-judge — backend-api-spec §3 处理流程。

状态机 D5：queued → processing → completed | failed（终态不可变，BE-47）；
每次迁移写 judgment_events（CM-03 断言迁移序列）。
所有终态写入都走乐观守卫 UPDATE ... WHERE status IN ('queued','processing')，
行数为 0 说明已被并发方或回收器终结——放弃写入并记日志。

失败路径错误码：
- LLM_VALIDATION_FAILED     重试耗尽仍产出非法引用（retry_count=MAX_RETRIES-1）
- LLM_PROVIDER_TIMEOUT      provider 超时（退避重试后仍失败，REL-03）
- LLM_PROVIDER_RATE_LIMITED provider 429（同上）
- ESPLORA_UNAVAILABLE       图数据源完全不可用（REL-05）
- TASK_TIMEOUT              僵尸回收（BE-40）
- TASK_FAILED               其余未预期异常
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import anyio
from sqlalchemy import select, update

from ..core.config import get_settings
from ..graph_builder.builder import GraphBuilder
from ..graph_builder.data_source import build_provider
from ..llm_judge.judge import (
    BUILDER_VERSION,
    MAX_RETRIES,
    PROMPT_VERSION,
    InMemoryCache,
    JudgmentValidationError,
    LLMJudge,
    canonical_subgraph_hash,
)
from ..llm_judge.providers import get_llm_client
from ..models.base import (
    TERMINAL_STATUSES,
    CaseAddress,
    Judgment,
    JudgmentEvent,
)
from ..models.knowledge import CoinjoinTxid
from ..retrieval.retriever import Retriever, subgraphresult_to_canonical


class ProviderFailure(RuntimeError):
    """LLM provider 层故障（超时/限流），区别于输出校验失败（REL-03）。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class DataSourceUnavailable(RuntimeError):
    """上游图数据源完全不可用（REL-05）。"""


def _record_event(session, judgment_id: str, from_status: str | None,
                  to_status: str) -> None:
    session.add(JudgmentEvent(judgment_id=judgment_id,
                              from_status=from_status, to_status=to_status))


def _provider_error_code(exc: BaseException) -> str | None:
    """把 provider 层异常映射为明确 error_code；非 provider 异常返回 None。"""
    try:
        import httpx
    except ImportError:  # pragma: no cover — httpx 是 fastapi 必然依赖
        return None
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError,
                        httpx.TimeoutException)):
        return "LLM_PROVIDER_TIMEOUT"
    if isinstance(exc, httpx.HTTPStatusError) \
            and exc.response.status_code == 429:
        return "LLM_PROVIDER_RATE_LIMITED"
    if isinstance(exc, httpx.HTTPError):
        return "LLM_PROVIDER_ERROR"
    return None


PROVIDER_ATTEMPTS = 3  # REL-03：超时/限流先退避重试再进终态


def _engine():
    from sqlalchemy import create_engine

    url = get_settings().database_url
    return create_engine(url.replace("postgresql://", "postgresql+psycopg://"),
                         pool_pre_ping=True)


def _judgment_cache(settings):
    """Redis 可达则用 Redis（跨进程共享），否则进程内降级（开发/测试）。"""
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url,
                                      socket_connect_timeout=1)
        client.ping()
        return client
    except Exception:  # noqa: BLE001 — 无 Redis 是合法部署形态
        return InMemoryCache()


def _label_sets(session) -> set[str]:
    # 跨链判定不再来自 DB 标签表（crosschain_tx_set 已移除）：GraphBuilder 只依赖
    # 运行时 CrosschainDetector。此处仅保留 CoinJoin 显式标记集合。
    return set(session.execute(select(CoinjoinTxid.txid)).scalars().all())


async def run_analysis(judgment_id: str, session=None) -> str | None:
    """执行完整分析管线；返回最终状态名（completed/failed/skipped）。"""
    own_session = session is None
    engine = _engine() if own_session else None
    try:
        if own_session:
            from sqlalchemy.orm import Session

            session = Session(engine)
        settings = get_settings()
        row = session.get(Judgment, judgment_id)
        if row is None:
            return "skipped"
        # BE-47 状态守卫：终态不可变，重放任务消息直接拒绝
        if row.status in TERMINAL_STATUSES:
            print(f"[orchestration] judgment {judgment_id} already terminal "
                  f"({row.status}); refusing replay")
            return "skipped"

        prior_status = row.status  # 先取值：update 的 synchronize_session 会改写已加载对象
        # 只从 queued 抢占：重放消息撞上 processing 行直接 skipped，
        # 否则第二个执行方会 claim 已在跑的行并双跑整个管线（issue #22）
        claimed = session.execute(
            update(Judgment)
            .where(Judgment.id == judgment_id,
                   Judgment.status == "queued")
            .values(status="processing"))
        if claimed.rowcount == 0:  # 并发竞争下被他人处理/终结
            session.rollback()
            return "skipped"
        _record_event(session, judgment_id, prior_status, "processing")
        session.commit()

        started = time.perf_counter()
        try:
            return await _execute(session, row, settings, started)
        except JudgmentValidationError as exc:
            _mark_failed(session, judgment_id, "LLM_VALIDATION_FAILED",
                         str(exc), retry_count=MAX_RETRIES - 1)
            return "failed"
        except ProviderFailure as exc:
            _mark_failed(session, judgment_id, exc.code,
                         f"provider failure after "
                         f"{PROVIDER_ATTEMPTS} attempts")
            return "failed"
        except DataSourceUnavailable as exc:
            _mark_failed(session, judgment_id, "ESPLORA_UNAVAILABLE", str(exc))
            return "failed"
        except Exception as exc:  # noqa: BLE001 — 编排层兜底任何管线异常
            code = _provider_error_code(exc) or "TASK_FAILED"
            _mark_failed(session, judgment_id, code, repr(exc))
            return "failed"
    finally:
        if own_session:
            session.close()
            engine.dispose()


def _sync_seed_block_time(provider, address: str) -> float | None:
    """同步取种子地址的最近区块时间（供 live/fixture 共用）。"""
    return provider.seed_block_time(address) if hasattr(provider, "seed_block_time") else None


async def _isolated_sync(live: bool, fn, /, *args, **kwargs):
    """live 模式把同步图构建隔离到线程池，避免阻塞 FastAPI 事件循环。

    issue #12：实时 Esplora 网络请求/重试等待不得阻塞事件循环。这里用
    ``anyio.to_thread.run_sync`` —— 与 Starlette TestClient 的 anyio blocking
    portal 同一线程模型 —— 而非 stdlib ``asyncio.to_thread`` 的默认事件循环池
    （后者在 Python 3.12 + TestClient 的 create_task 后台任务路径下会令管线卡死
    在 processing，CI 复现并已回退，见 763668d）。

    fixture 模式（graph_data_mode != "live"）保持同步：构建 ~0.4ms，可忽略阻塞，
    且不引入线程切换，避免打破既有测试/CI。
    """
    if live:
        return await anyio.to_thread.run_sync(fn, *args, **kwargs)
    return fn(*args, **kwargs)


async def _execute(session, row: Judgment, settings, started: float) -> str:
    provider, _seeds = build_provider(settings)

    coinjoin_txids = _label_sets(session)
    if hasattr(provider, "coinjoin_txids"):
        # fixture 数据源自带夹具级标记集（demo txid 不在真实标签表里）
        coinjoin_txids = coinjoin_txids | provider.coinjoin_txids

    builder = GraphBuilder(coinjoin_txids=coinjoin_txids)
    # out_of_range 终止需要时间窗基准（spec §3）：种子最近活动时刻；
    # live 模式下该取值会进 Redis 缓存，BFS 首次展开直接命中。
    # issue #12：live 模式（graph_data_mode == "live"）的网络请求/重试等待经
    # _isolated_sync 隔离到 anyio 线程池，不阻塞事件循环；fixture 模式保持同步
    # （构建 ~0.4ms，可忽略阻塞），避免引入线程切换破坏既有测试/CI。
    live = settings.graph_data_mode == "live"
    seed_time = await _isolated_sync(
        live, _sync_seed_block_time, provider, row.address)
    # issue #7 data_as_of：本次分析使用的链上数据时间点（种子/发起区块时间）；
    # 无法取得统一链上时间时，记录本次查询时间（now UTC）。
    data_as_of = (
        datetime.fromtimestamp(seed_time, UTC) if seed_time is not None
        else datetime.now(UTC)
    )
    subgraph = await _isolated_sync(
        live, builder.build, row.address, provider,
        hops=row.hops, time_window_days=row.time_window_days,
        seed_block_time=seed_time)
    if subgraph.stats.degraded and len(subgraph.nodes) <= 1:
        # REL-05：所有扩展单元都失败 → 数据源完全分区，显式进终态
        raise DataSourceUnavailable(
            f"tx provider failed for every expansion of {row.address}")

    canon = subgraphresult_to_canonical(subgraph)

    retriever = Retriever(session, settings)
    retrieval = retriever.retrieve(canon)

    judge = LLMJudge(client=get_llm_client(settings),
                     cache=_judgment_cache(settings))
    verdict = None
    provider_code: str | None = None
    # issue #8：图不完整时把数据质量事实传给 LLM（谨慎判断、说明局限）
    graph_degraded = bool(subgraph.stats.degraded)
    missing_branches = int(subgraph.stats.missing_branches)
    for attempt in range(PROVIDER_ATTEMPTS):
        try:
            verdict = await judge.judge(
                address=row.address, subgraph=canon,
                candidates=retrieval.candidates,
                builder_version=BUILDER_VERSION, model=settings.llm_model,
                degraded=graph_degraded, missing_branches=missing_branches)
            break
        except Exception as exc:
            code = _provider_error_code(exc)
            if code is None:
                raise  # JudgmentValidationError 等交给上层语义化处理
            provider_code = code
            await asyncio.sleep(0.05 * (attempt + 1))  # 退避后重试（REL-03）
    if verdict is None:
        raise ProviderFailure(provider_code or "LLM_PROVIDER_ERROR")

    matched = next((c for c in retrieval.candidates
                    if c.name == verdict.matched_pattern), None)
    latency_ms = int((time.perf_counter() - started) * 1000)
    result = session.execute(
        update(Judgment)
        .where(Judgment.id == row.id,
               Judgment.status.in_(("queued", "processing")))
        .values(
            status="completed",
            subgraph_snapshot=canon,
            subgraph_hash=canonical_subgraph_hash(canon),
            risk_level=verdict.risk_level,
            matched_pattern_id=matched.pattern_id if matched else None,
            matched_pattern_name=verdict.matched_pattern,
            confidence=float(verdict.confidence),
            evidence=list(verdict.evidence),
            reasoning=verdict.reasoning,
            recommended_action=verdict.recommended_action,
            model=settings.llm_model,
            prompt_version=PROMPT_VERSION,
            builder_version=BUILDER_VERSION,
            latency_ms=latency_ms,
            thinking=judge.last_thinking or None,
            concluded_at=datetime.now(UTC),
            data_as_of=data_as_of,
            data_quality=subgraph.stats.data_quality,
            requires_manual_review=subgraph.stats.requires_manual_review,
        ))
    if result.rowcount == 0:  # 终态守卫：并发方已终结该任务
        session.rollback()
        return "skipped"
    _record_event(session, row.id, "processing", "completed")
    # issue #7：CaseAddress.judgment_id 的明确写入/更新逻辑——完成一个 judgment 后，
    # 把该地址在所有案件中的关联行指向其「最新 completed」judgment（时间版本化指针）。
    latest_id = session.execute(
        select(Judgment.id)
        .where(Judgment.address == row.address,
               Judgment.status == "completed")
        .order_by(Judgment.created_at.desc(), Judgment.id.desc())
        .limit(1)).scalar_one_or_none()
    if latest_id:
        session.execute(
            update(CaseAddress)
            .where(CaseAddress.address == row.address)
            .values(judgment_id=latest_id))
    session.commit()
    return "completed"


def _mark_failed(session, judgment_id: str, error_code: str,
                 error_message: str, retry_count: int | None = None) -> None:
    values = {
        "status": "failed",
        "error_code": error_code,
        "error_message": error_message[:2000],
        "failed_at": datetime.now(UTC),
        "concluded_at": datetime.now(UTC),
    }
    if retry_count is not None:
        values["retry_count"] = retry_count
    result = session.execute(
        update(Judgment)
        .where(Judgment.id == judgment_id,
               Judgment.status.in_(("queued", "processing")))
        .values(**values))
    if result.rowcount == 0:
        session.rollback()
        print(f"[orchestration] judgment {judgment_id} finished elsewhere; "
              "failure write skipped")
        return
    _record_event(session, judgment_id, "processing", "failed")
    session.commit()


def reclaim_zombies(session, *, older_than_seconds: int | None = None) -> list[str]:
    """BE-40 僵尸回收：updated_at 超时的 queued/processing 行判为 failed。

    由 analyze 入口顺带触发（摊薄成本），也可挂定时任务。
    """
    from datetime import timedelta

    older = older_than_seconds or get_settings().zombie_timeout_seconds
    cutoff = datetime.now(UTC) - timedelta(seconds=older)
    ids = session.execute(
        select(Judgment.id)
        .where(Judgment.status.in_(("queued", "processing")),
               Judgment.updated_at < cutoff)).scalars().all()
    for jid in ids:
        _mark_failed(session, jid, "TASK_TIMEOUT",
                     f"no progress for {older}s; reclaimed")
    return ids
