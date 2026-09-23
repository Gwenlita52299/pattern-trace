"""万级压测数据灌入 — STRESS 压测与 PERF-02 共用的数据前提（stress-test-spec §2.2）。

直接 ORM bulk insert（不走 API）：fixture 只有 3 个种子地址，经 analyze 全链路
到不了万级且耗时不可接受。合成快照为 canonical 形状（seed_address/nodes/edges/
stats），24 节点默认档 JSON 体积 ~5KB，足以触发 TOAST LZ4 压缩（PERF-02 口径）。

幂等：地址命名空间 STRESS 前缀 + 计数补差，重复执行只补不足部分，不产生重复行。
**热档特殊**：spec §2.3 要求轮询 id 池分热/冷两档，热档须是「最近 1 小时内完成」
的判定——旧的热档行会自然滑出窗口，所以热档按「按标记重建」实现（每次删掉
带热标记的 STRESS 行后重灌 SEED_HOT_JUDGMENTS 行），计数与新鲜度都可重复。
同时幂等 seed 压测账号（复用 backend.api.app.seed_user 的 DB upsert）——locustfile
用它登录，backend 容器与宿主机脚本共享同一 DB，因此账号对 HTTP API 可见。

环境/参数：
    DATABASE_URL        默认 postgresql://pt:pt@localhost:5432/patterntrace
    SEED_JUDGMENTS      目标 judgments 行数（不含热档），默认 10000
    SEED_HOT_JUDGMENTS  热档行数，默认 500
    SEED_CASES          目标 cases 行数，默认 100
    SEED_SNAPSHOT_NODES 快照节点数（payload 体积旋钮），默认 24
执行：
    python tests/performance/seed_volume.py            # 灌数（幂等）
    python tests/performance/seed_volume.py --cleanup   # 清理 STRESS 命名空间
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
# 与 perf_phase6.py 同一测试密钥：Settings 必填项，灌数不校验 JWT 但缺省会 fail-fast
os.environ.setdefault("JWT_SECRET",
                      "0123456789abcdef0123456789abcdef"
                      "0123456789abcdef0123456789abcdef")

STRESS_EMAIL = "stress@perf.local"
STRESS_PASSWORD = "StressPerf!2026"
ADDR_PREFIX = "STRESS"
N_DISTINCT_ADDR = 1000  # 地址复用度 10 judgments/addr，贴近真实「同地址反复分析」分布
N_TEMPLATES = 5         # 快照模板数：同模板行共享一份 JSON 对象，内存可控
HOT_MARKER = "stress seed hot row"   # 热档行标记（清理与重建都靠它，不靠时间猜）
HOT_WINDOW_MINUTES = 30

RISK_CYCLE = ["high", "medium", "low", "no_match"]
ACTION_CYCLE = ["freeze", "monitor", "none"]


def stress_address(i: int) -> str:
    # 32 chars，符合 address 列 String(62)；仅作查询键，不经 BTC 校验
    return f"{ADDR_PREFIX}{i:06d}" + "x" * 20


def build_snapshot(template_idx: int, n_nodes: int) -> dict:
    """canonical 子图（backend/retrieval/retriever.py:subgraphresult_to_canonical 键集）。"""
    tid = f"{template_idx:03d}"
    nodes, edges = [], []
    root = f"addr:stress{tid}a0000"
    nodes.append({"id": root, "kind": "address",
                  "label": f"stress{tid}a0000", "first_layer": 0,
                  "value_btc": 12.5})
    prev = root
    for i in range(1, n_nodes):
        if i % 2:
            nid = f"tx:stress{tid}t{i:04d}"
            nodes.append({"id": nid, "kind": "tx", "label": nid.split(":", 1)[1],
                          "first_layer": (i + 1) // 2, "value_btc": 0.5,
                          "block_height": 800_000 + i})
        else:
            nid = f"addr:stress{tid}a{i:04d}"
            nodes.append({"id": nid, "kind": "address", "label": nid.split(":", 1)[1],
                          "first_layer": i // 2, "value_btc": 0.5})
        edges.append({"id": f"edge:{prev}->{nid}", "source": prev,
                      "target": nid, "value_btc": 0.5})
        prev = nid
    return {
        "seed_address": root,
        "nodes": sorted(nodes, key=lambda n: n["id"]),
        "edges": sorted(edges, key=lambda e: e["id"]),
        "stats": {"node_count": len(nodes), "edge_count": len(edges),
                  "max_first_layer": n_nodes // 2, "data_quality": "complete",
                  "requires_manual_review": False, "missing_branches": 0,
                  "source_errors": []},
    }


def _snapshot_bytes(snap: dict) -> int:
    return len(json.dumps(snap).encode())


def cleanup() -> int:
    """删除 STRESS 命名空间的压测数据（judgments + cases + case_addresses）。

    压测结束必须执行：否则 1 万行 STRESS 判定会污染 GET /cases、本地演示与
    其他测试的历史记录（spec §2.2）。reports 经 case 级联删除。
    """
    from sqlalchemy import delete, func, select
    from sqlalchemy.orm import Session

    from backend.api.app import get_db_engine
    from backend.models.base import Case, CaseAddress, Judgment

    engine = get_db_engine()
    with Session(engine) as session:
        case_ids = select(Case.id).where(Case.title.like(f"{ADDR_PREFIX}%"))
        n_addr = session.execute(
            delete(CaseAddress).where(CaseAddress.case_id.in_(case_ids))
        ).rowcount
        n_cases = session.execute(
            delete(Case).where(Case.title.like(f"{ADDR_PREFIX}%"))).rowcount
        n_judgments = session.execute(
            delete(Judgment).where(Judgment.address.like(f"{ADDR_PREFIX}%"))
        ).rowcount
        session.commit()

        left_judgments = session.execute(
            select(func.count(Judgment.id))
            .where(Judgment.address.like(f"{ADDR_PREFIX}%"))).scalar_one()
        left_cases = session.execute(
            select(func.count(Case.id))
            .where(Case.title.like(f"{ADDR_PREFIX}%"))).scalar_one()
    print(f"cleanup: removed case_addresses={n_addr} cases={n_cases} "
          f"judgments={n_judgments}")
    print(f"residue: judgments={left_judgments} cases={left_cases}")
    return 1 if (left_judgments or left_cases) else 0


def _seed_hot(session, n_hot: int, templates: list[dict],
              snap_hash: list[str], rng: random.Random) -> int:
    """重建热档：删标记行 + 重灌（保证落在热窗口内，见模块 docstring）。"""
    from sqlalchemy import delete

    from backend.models.base import Judgment

    deleted = session.execute(
        delete(Judgment).where(Judgment.reasoning == HOT_MARKER,
                               Judgment.address.like(f"{ADDR_PREFIX}%"))
    ).rowcount
    now = datetime.now(timezone.utc)
    batch: list[Judgment] = []
    for i in range(n_hot):
        t = i % N_TEMPLATES
        batch.append(Judgment(
            id=str(uuid.uuid4()),
            address=stress_address(i % N_DISTINCT_ADDR),
            hops=3, time_window_days=90,
            status="completed",
            subgraph_snapshot=templates[t],
            subgraph_hash=snap_hash[t],
            risk_level=RISK_CYCLE[i % len(RISK_CYCLE)],
            confidence=round(rng.uniform(0.6, 0.95), 4),
            evidence=[],
            reasoning=HOT_MARKER,
            recommended_action=ACTION_CYCLE[i % len(ACTION_CYCLE)],
            model="stress-seed",
            latency_ms=rng.randint(800, 3000),
            created_at=now - timedelta(seconds=rng.randint(0, HOT_WINDOW_MINUTES * 60)),
            updated_at=now,
        ))
        if len(batch) >= 1000:
            session.add_all(batch)
            session.commit()
            batch = []
    if batch:
        session.add_all(batch)
        session.commit()
    print(f"hot tier: rebuilt (deleted={deleted} inserted={n_hot}, "
          f"window={HOT_WINDOW_MINUTES}min)")
    return n_hot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STRESS 压测数据灌入/清理")
    parser.add_argument("--cleanup", action="store_true",
                        help="删除 STRESS 命名空间数据后退出")
    parser.add_argument("--judgments", type=int,
                        default=int(os.environ.get("SEED_JUDGMENTS", "10000")))
    parser.add_argument("--hot", type=int,
                        default=int(os.environ.get("SEED_HOT_JUDGMENTS", "500")))
    parser.add_argument("--cases", type=int,
                        default=int(os.environ.get("SEED_CASES", "100")))
    parser.add_argument("--snapshot-nodes", type=int,
                        default=int(os.environ.get("SEED_SNAPSHOT_NODES", "24")))
    args = parser.parse_args(argv)

    if args.cleanup:
        return cleanup()

    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from backend.api.app import get_db_engine, seed_user
    from backend.models.base import Case, CaseAddress, Judgment, User

    # 账号 upsert：重复执行覆盖密码，保证与 locustfile 默认凭据一致
    seed_user(STRESS_EMAIL, STRESS_PASSWORD)

    templates = [build_snapshot(t, args.snapshot_nodes) for t in range(N_TEMPLATES)]
    snap_hash = [hashlib.sha256(json.dumps(s, sort_keys=True).encode()).hexdigest()
                 for s in templates]
    print(f"snapshot templates: {N_TEMPLATES} × {args.snapshot_nodes} nodes, "
          f"{_snapshot_bytes(templates[0])} bytes each")

    rng = random.Random(20260906)  # 固定种子：created_at 分布可复现
    now = datetime.now(timezone.utc)
    engine = get_db_engine()

    with Session(engine) as session:
        uid = session.execute(
            select(User.id).where(User.email == STRESS_EMAIL)).scalar_one()

        # 冷档口径：不计热标记行，避免热档把 bulk 目标顶掉（见 docstring）
        done = session.execute(
            select(func.count(Judgment.id))
            .where(Judgment.address.like(f"{ADDR_PREFIX}%"),
                   Judgment.reasoning != HOT_MARKER)).scalar_one()
        deficit = max(args.judgments - done, 0)
        print(f"judgments(bulk): existing={done} target={args.judgments} "
              f"inserting={deficit}")

        batch: list[Judgment] = []
        for offset in range(deficit):
            i = done + offset
            addr_idx = i % N_DISTINCT_ADDR
            t = i % N_TEMPLATES
            batch.append(Judgment(
                id=str(uuid.uuid4()),
                address=stress_address(addr_idx),
                hops=3, time_window_days=90,
                status="completed",
                subgraph_snapshot=templates[t],
                subgraph_hash=snap_hash[t],
                risk_level=RISK_CYCLE[i % len(RISK_CYCLE)],
                confidence=round(rng.uniform(0.6, 0.95), 4),
                evidence=[],
                reasoning="stress seed row",
                recommended_action=ACTION_CYCLE[i % len(ACTION_CYCLE)],
                model="stress-seed",
                latency_ms=rng.randint(800, 3000),
                created_at=now - timedelta(seconds=rng.randint(0, 30 * 86400)),
                updated_at=now - timedelta(seconds=rng.randint(0, 30 * 86400)),
            ))
            if len(batch) >= 2000:
                session.add_all(batch)
                session.commit()
                batch = []
        if batch:
            session.add_all(batch)
            session.commit()

        if args.hot:
            _seed_hot(session, args.hot, templates, snap_hash, rng)

        case_done = session.execute(
            select(func.count(Case.id))
            .where(Case.title.like(f"{ADDR_PREFIX}%"))).scalar_one()
        case_deficit = max(args.cases - case_done, 0)
        print(f"cases: existing={case_done} target={args.cases} "
              f"inserting={case_deficit}")
        for ci in range(case_deficit):
            i = case_done + ci
            case = Case(id=str(uuid.uuid4()), owner_id=uid,
                        title=f"STRESS case {i:04d}",
                        description="stress seed case",
                        created_at=now - timedelta(days=i),
                        updated_at=now - timedelta(days=i))
            session.add(case)
            # 每案 5 个地址：既触发 GET /cases/{id} 的逐地址 latest-judgment 关联查询，
            # 又不与 1000 地址命名空间撞 unique 约束（若存在）
            for a in range(5):
                addr = stress_address((i * 5 + a) % N_DISTINCT_ADDR)
                session.add(CaseAddress(
                    id=str(uuid.uuid4()), case_id=case.id, address=addr,
                    label="stress", added_at=now - timedelta(days=i)))
            if (ci + 1) % 50 == 0:
                session.commit()
        session.commit()

        total = session.execute(select(func.count(Judgment.id))).scalar_one()
        print(f"done. judgments total in table = {total}")
        if total < 10_000:
            print("WARNING: total < 10k — PERF-02/STRESS 口径要求 ≥10k，"
                  "若表中还有非 STRESS 数据不足，请调大 SEED_JUDGMENTS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
