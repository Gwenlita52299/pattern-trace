"""阶段5 种子案例：high / low / no_match 三档各一（排期完成标志）。

幂等：按案件标题判存——已存在且关联地址已有 completed judgment 时跳过，
可安全重复执行。判定走完整管线（fixture 数据源 + mock provider），
三案使用不同 seed 地址 → 不同 subgraph_hash → 判决缓存互不串扰。

用法：LLM_PROVIDER=mock python -m backend.services.seed_cases
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _new_id() -> str:
    return str(uuid.uuid4())

SEED_CASES = [
    # (标题, fixture seed 序号, hops, mock scenario, 描述)
    ("Lazarus 高风险演示", 0, 3, "valid_high",
     "CoinJoin 入口 + 分层 peel + 跨链逃逸的高风险演示地址"),
    ("低风险普通钱包", 1, 3, "valid_low",
     "无混币器接触的普通消费链路"),
    ("无匹配模式地址", 2, 3, "valid_no_match",
     "平凡转账结构，检索与判断均不命中已知模式"),
]


def _clear_judgment_cache() -> None:
    """scenario 切换不影响缓存 key（key 只含 model/prompt_version/graph hash）；
    三案图结构不同天然隔离，这里清缓存只为重跑时强制走最新 prompt。"""
    try:
        import redis

        from ..core.config import get_settings

        client = redis.Redis.from_url(get_settings().redis_url,
                                      socket_connect_timeout=1)
        keys = list(client.scan_iter(match="gb-v1:*", count=100))
        if keys:
            client.delete(*keys)
    except Exception:  # noqa: BLE001 — 无 Redis 时为进程内缓存
        pass


async def run(verbose: bool = True) -> list[dict]:
    os.environ.setdefault("JWT_SECRET", "migration-placeholder")
    os.environ["LLM_PROVIDER"] = "mock"
    os.environ["GRAPH_DATA_MODE"] = "fixture"

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from ..api.app import get_db_engine, seed_user
    from ..core.config import reset_settings
    from ..graph_builder.data_source import FixtureTxProvider
    from ..models.base import Case, CaseAddress, Judgment
    from .orchestration import run_analysis as _run_pipeline

    reset_settings()  # 让上面的环境变量在 Settings 里生效
    seeds = FixtureTxProvider.load().seed_addresses

    engine = get_db_engine()
    investigator_email = "investigator@patterntrace.local"
    # 幂等 seed 演示账号（密码仅本地演示环境）
    seed_user(investigator_email, "PatternDemo!2026", role="investigator")
    user = None
    with Session(engine) as session:
        from sqlalchemy import text

        user = session.execute(text(
            "SELECT id FROM users WHERE email = :e"),
            {"e": investigator_email}).mappings().first()

    results = []
    for title, seed_idx, hops, scenario, description in SEED_CASES:
        address = seeds[seed_idx]
        with Session(engine) as session:
            existing = session.execute(
                select(Case).where(Case.title == title)).scalar_one_or_none()
        if existing is not None:
            linked = session.execute(
                select(CaseAddress.address).where(
                    CaseAddress.case_id == existing.id,
                    CaseAddress.address == address)).scalar_one_or_none()
            done = session.execute(
                select(Judgment.id).where(
                    Judgment.address == address,
                    Judgment.status == "completed")
                .limit(1)).scalar_one_or_none()
            if linked and done:
                results.append({"case": title, "status": "skipped",
                                "risk": None})
                continue
            case_id = existing.id
        else:
            with Session(engine) as session:
                session.add(Case(
                    id=_new_id(), owner_id=user["id"] if user else None,
                    title=title, description=description))
                session.commit()
                case_id = session.execute(
                    select(Case.id).where(Case.title == title)).scalar_one()
            with Session(engine) as session:
                session.add(CaseAddress(id=_new_id(), case_id=case_id,
                                        address=address))
                session.commit()

        os.environ["LLM_MOCK_SCENARIO"] = scenario
        _clear_judgment_cache()
        t0 = time.perf_counter()
        await _run_pipeline(_enqueue_judgment(address, hops))
        elapsed = time.perf_counter() - t0
        os.environ.pop("LLM_MOCK_SCENARIO", None)

        with Session(engine) as session:
            j = session.execute(
                select(Judgment)
                .where(Judgment.address == address)
                .order_by(Judgment.created_at.desc())
                .limit(1)).scalar_one_or_none()
            risk = j.risk_level if j else None
        results.append({"case": title, "status": risk or "pending",
                        "risk": risk})
        if verbose:
            print(f"  seed case {title!r}: risk={risk} ({elapsed:.1f}s)")

    return results


def _new_id() -> str:
    return str(uuid.uuid4())


def _enqueue_judgment(address: str, hops: int) -> str:
    """为该地址建一条 queued judgment 行，返回 id（种子脚本直接驱动管线）。"""
    from sqlalchemy.orm import Session

    from ..api.app import get_db_engine
    from ..models.base import Judgment

    with Session(get_db_engine()) as session:
        row = Judgment(id=_new_id(), address=address, hops=hops,
                       time_window_days=90, status="queued")
        session.add(row)
        session.commit()
        return row.id


if __name__ == "__main__":
    summary = asyncio.run(run())
    # 成功口径：跳过（已就绪）或产出三档之一的 verdict
    ok = all(r["status"] == "skipped"
             or r.get("risk") in ("high", "low", "no_match")
             for r in summary)
    print(f"seed cases: {summary}")
    raise SystemExit(0 if ok else 1)
