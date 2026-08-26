"""负样本构造 — ingest-spec §3 / §3a / IG-04~06。

普通钱包子图（无混币器接触、无黑名单标签、频率正常），按 3:1 负正比例
生成，写入独立 pattern_negatives 表（结构与 patterns 一致，无向量索引）。
**绝不写入 patterns 业务召回库**——避免"正常钱包 pattern"成为 judge 候选。

spec §3 的"从公开浏览器抓取"模式为 P2（live 模式，复用 graph_builder
EsploraClient）；当前实现确定性合成模式，满足全部 IG 约束且可复现。

确定性：内容只依赖 (seed_key, slot)——重跑同参数产出相同 content_hash，
upsert 幂等（IG-10/17）。
"""
from __future__ import annotations

import random

try:  # 包内运行（python -m ingest.xxx）
    from .common import new_pattern_row, upsert_rows
    from .corpus_gen import _fake_addr, _Sub
except ImportError:  # 直接运行（python ingest/generate_negatives.py）
    from common import new_pattern_row, upsert_rows
    from corpus_gen import _fake_addr, _Sub


def _salary_spender(rng: random.Random, box: _Sub) -> None:
    """工资入账 → 日常消费散出。"""
    n_bills = rng.randint(3, 6)
    targets = box.spend(rng, box.sub.seed_address,
                        [_fake_addr(rng) for _ in range(n_bills)],
                        first_layer=0)
    for addr in targets[:2]:
        box.spend(rng, addr, [None], first_layer=1)


def _exchange_user(rng: random.Random, box: _Sub) -> None:
    """交易所存提循环：中转地址多进多出。"""
    hub = _fake_addr(rng)
    box.spend(rng, box.sub.seed_address, [hub], first_layer=0)
    outs = box.spend(rng, hub, [_fake_addr(rng) for _ in range(rng.randint(2, 4))],
                     first_layer=1)
    for addr in outs[:2]:
        box.spend(rng, addr, [None], first_layer=2)


def _dormant_savings(rng: random.Random, box: _Sub) -> None:
    """低频大额：长期休眠后一次性转出。"""
    box.spend(rng, box.sub.seed_address, [_fake_addr(rng)], first_layer=0)
    box.spend(rng, box.sub.seed_address,
              [None, _fake_addr(rng)][: rng.randint(1, 2)], first_layer=0)


_TEMPLATES = {"salary": _salary_spender, "exchange": _exchange_user,
              "dormant": _dormant_savings}


def generate_one(seed_key: int, slot: int) -> tuple:
    rng = random.Random(f"normal-neg:{seed_key}:{slot}")
    template = rng.choice(list(_TEMPLATES))
    seed_addr = "bc1qn" + _fake_addr(rng)[4:]  # 独立命名空间，便于审计区分
    box = _Sub(seed_addr)
    _TEMPLATES[template](rng, box)
    name = f"normal_{template}"
    return box.sub, name


def generate(count: int, seed_key: int = 42) -> list[dict]:
    rows, seen = [], set()
    for slot in range(count):
        sub, name = generate_one(seed_key, slot)
        while sub.seed_address in seen:
            sub.seed_address = "bc1qn" + _fake_addr(
                random.Random(f"neg-rekey:{seed_key}:{slot}:{len(seen)}"))[4:]
        seen.add(sub.seed_address)
        rows.append(new_pattern_row(
            name=name, source="constructed_normal", grade="B", sub=sub))
    return rows


def count_positives(session) -> int:
    """正样本口径（IG-05 统一定义）：patterns 表中 evidence_grade='A' 的行
    （含 lazarus_confirmed 与 lazarus_synth，验证脚本会分别打印两种来源计数）。"""
    from sqlalchemy import func

    from backend.models.knowledge import Pattern

    return session.query(func.count(Pattern.id)).filter(
        Pattern.evidence_grade == "A").scalar() or 0


def run(session, ratio: int = 3, seed_key: int = 42,
        target: int | None = None, verbose: bool = True) -> dict:
    """target 缺省 = round(ratio × 正样本数)。"""
    from backend.models.knowledge import PatternNegative

    if target is None:
        target = round(ratio * count_positives(session))
    rows = generate(target)
    upserted = upsert_rows(session, PatternNegative, rows,
                           ["seed_address", "content_hash"])
    if verbose:
        print(f"negatives: target={target} upserted={upserted}")
    return {"target": target, "upserted": upserted}


if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.environ.setdefault("JWT_SECRET", "migration-placeholder")

    try:
        from .common import get_engine
    except ImportError:  # 直接运行回退
        from common import get_engine
    from sqlalchemy.orm import Session

    from backend.models.knowledge import PatternNegative  # noqa: F401

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    engine = get_engine()
    with Session(engine) as s:
        run(s, target=n or None)
