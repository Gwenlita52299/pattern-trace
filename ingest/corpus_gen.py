"""参数化 Lazarus playbook 语料生成器 — 补充 ingest-spec §2 的规模要求。

背景：spec 预期「数千条正样本」，但本地 bybit_rust 只有 golden fixture
（3 个 seed）。本模块从已确认场景的拓扑特征（CoinJoin 入口 → 分层 peel /
扇出 → 跨链桥逃逸）派生结构变体，补足检索阶段需要的知识库规模。

诚实标注：生成 pattern 的 source='lazarus_synth'、provenance='synthetic'——
**不是链上实证**。issue #10 起 grade 用 'S'（synthetic template，区别于真实样本的
A/B 语义），合成模板只作结构检索参考，不会被解释为真实确认证据；真实 golden
场景由 load_lazarus_subgraphs.py 以 source='lazarus_confirmed'、
provenance='confirmed'、grade='A' 入库，二者永不混淆。

确定性：每个实例只依赖 (seed, index) —— 同参数重跑产出完全相同的内容，
配合 (seed_address, content_hash) upsert 实现幂等（IG-10）。
"""
from __future__ import annotations

import random

from backend.graph_builder.id_contract import edge_id, node_id

try:  # 包内运行（python -m ingest.xxx）
    from .common import MIN_PATTERN_NODES, Subgraph, new_pattern_row, to_canonical
except ImportError:  # 直接运行（python ingest/corpus_gen.py）
    from common import MIN_PATTERN_NODES, Subgraph, new_pattern_row, to_canonical

# 协议词表取自基线 op_return_decoded 真实分布（runes/thorchain 占绝对多数）
CROSSCHAIN_PROTOCOLS = ["runes", "thorchain_swap", "thorchain_outbound",
                        "crosschain_bridge"]
BECH32_ALPHABET = "023456789acdefghjklmnpqrstuvwxyz"


def _fake_addr(rng: random.Random) -> str:
    return "bc1q" + "".join(rng.choice(BECH32_ALPHABET) for _ in range(38))


def _fake_txid(rng: random.Random) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(64))


class _Sub:
    """增量构建一个 Subgraph：地址节点去重、tx 边成对展开（addr→tx→dst）。"""

    def __init__(self, seed_address: str):
        self.sub = Subgraph(seed_address=seed_address)
        self._addrs: set[str] = set()
        # seed 地址自身必须是图节点（否则所有边 source 悬挂）
        self.add_addr(seed_address, 0)

    def add_addr(self, addr: str, first_layer: int) -> None:
        if addr in self._addrs:
            return
        self._addrs.add(addr)
        self.sub.nodes.append({
            "id": node_id("address", addr), "kind": "address", "label": addr,
            "first_layer": first_layer,
            "total_received_btc": 0.0, "total_sent_btc": 0.0,
            "utxo_count": 1, "direct_related_to_lazarus": False,
        })

    def spend(self, rng: random.Random, src: str, dsts: list[str | None],
              *, first_layer: int, remixer: bool = False,
              protocol: str | None = None) -> list[str]:
        """src 经一笔交易付给 dsts（None = 终端输出，stopped）；返回实际目标。"""
        txid = _fake_txid(rng)
        n_in = rng.randint(1, 6)
        n_out_extra = rng.randint(0, 3)
        total_in = round(rng.uniform(0.05, 4.0), 8)
        fee_ratio = rng.uniform(0.002, 0.03)

        stopped = all(d is None for d in dsts)
        base = {
            "txid": txid, "tx_layer": f"tx{first_layer + 2}",
            "total_num_inputs": n_in,
            "total_num_outputs": len([d for d in dsts if d]) + n_out_extra,
            # 终端交易保留 tx 节点但不再展开——与基线 stopped 语义一致
            "is_stopped_expansion": stopped,
            "is_remixer": remixer,
            "is_crosschain": protocol is not None,
            "op_return_protocol": protocol,
        }
        src_node = node_id("address", src)
        self.sub.edges.append({
            **base,
            "id": edge_id(src_node, node_id("transaction", txid)),
            "source": src_node, "target": node_id("transaction", txid),
            "value_ratio": round(rng.uniform(0.6, 1.0), 8),
            "dst_value_btc": total_in * (1 - fee_ratio),
        })

        live = [d for d in dsts if d is not None]
        total_out = total_in * (1 - fee_ratio)
        weights = [rng.uniform(0.5, 1.5) for _ in live] or [1.0]
        wsum = sum(weights)
        targets: list[str] = []
        for d, w in zip(live, weights):
            self.add_addr(d, first_layer + 1)
            edge = {
                **base,
                "id": edge_id(node_id("transaction", txid), node_id("address", d)),
                "source": node_id("transaction", txid),
                "target": node_id("address", d),
                "value_ratio": round(total_out * w / wsum / total_in, 8),
                "dst_value_btc": round(total_out * w / wsum, 8),
            }
            self.sub.edges.append(edge)
            targets.append(d)
        return targets


def _peel_chain(rng: random.Random, sub: _Sub) -> None:
    """经典 peel chain：每层扇出后单线程延续，逐层剥离小额。"""
    layers = rng.randint(3, 5)
    fanout = rng.randint(2, 4)
    current = sub.sub.seed_address
    for layer in range(layers):
        dsts = [_fake_addr(rng) for _ in range(fanout)]
        if layer == layers - 1:
            dsts = [None] * fanout  # 末端全部沉淀
        elif layer > 0 and rng.random() < 0.25:
            # 中途跨链逃逸：资金桥出即链条终止（不能再用幽灵地址续链，
            # 否则产生无节点的悬挂边和退化小图）。layer0 不允许逃逸——
            # CoinJoin 入口是正样本的判定条件（IG-03 混币器接触）
            sub.spend(rng, current, [None], first_layer=layer,
                      protocol=rng.choice(CROSSCHAIN_PROTOCOLS))
            return
        targets = sub.spend(rng, current, dsts, first_layer=layer,
                            remixer=(layer == 0))
        current = targets[0] if targets else _fake_addr(rng)


def _fanout_split(rng: random.Random, sub: _Sub) -> None:
    """宽扇出拆分：mixer 出口一次性散到大量地址，各再走一层后休眠。"""
    wide = rng.randint(8, 16)
    exits = sub.spend(rng, sub.sub.seed_address,
                      [_fake_addr(rng) for _ in range(wide)],
                      first_layer=0, remixer=True)
    for addr in exits[: max(3, len(exits) // 2)]:  # 后半部分直接休眠，控制规模
        leaf = rng.choice([[None], [_fake_addr(rng)]])
        sub.spend(rng, addr, leaf, first_layer=1)


def _hybrid(rng: random.Random, sub: _Sub) -> None:
    """混合型：少数分支深 peel，其余浅层散开后跨链/沉淀。"""
    branches = rng.randint(3, 4)
    exits = sub.spend(rng, sub.sub.seed_address,
                      [_fake_addr(rng) for _ in range(branches)],
                      first_layer=0, remixer=True)
    deep = exits[0]
    for layer in range(rng.randint(2, 3)):
        targets = sub.spend(rng, deep, [_fake_addr(rng) for _ in range(2)],
                            first_layer=layer + 1)
        if not targets:
            break
        deep = targets[0]
    for addr in exits[1:]:
        if rng.random() < 0.4:
            sub.spend(rng, addr, [None], first_layer=1,
                      protocol=rng.choice(CROSSCHAIN_PROTOCOLS))
        else:
            sub.spend(rng, addr, [None], first_layer=1)


_TEMPLATES = {"peel_chain": _peel_chain, "fanout_split": _fanout_split,
              "hybrid": _hybrid}


def generate_one(seed_key: int, index: int) -> tuple[Subgraph, str]:
    rng = random.Random(f"lazarus-synth:{seed_key}:{index}")
    template = rng.choice(list(_TEMPLATES))
    seed_addr = _fake_addr(rng)
    box = _Sub(seed_addr)
    _TEMPLATES[template](rng, box)

    # 兜底：任何模板路径都不得低于正样本过滤线（§2 节点数 ≥ 5）。
    # 用同一 rng 流扩展 → 结果仍只依赖 (seed_key, index)，幂等不破
    while True:
        canon = to_canonical(box.sub)
        if canon["stats"]["node_count"] >= MIN_PATTERN_NODES:
            break
        addr = rng.choice(sorted(box._addrs))
        box.spend(rng, addr,
                  [_fake_addr(rng) for _ in range(rng.randint(2, 3))],
                  first_layer=canon["stats"]["max_first_layer"] + 1)

    # 命名只含形态学特征（模板+深度）：不同 seed 天然同名，
    # 使 IG-11 的「同名共存」由构造保证而非统计巧合
    name = f"synth_{template}_l{canon['stats']['max_first_layer']}"
    return box.sub, name


def generate(count: int, seed_key: int = 42) -> list[dict]:
    rows = []
    seen_seeds: set[str] = set()
    for i in range(count):
        sub, name = generate_one(seed_key, i)
        while sub.seed_address in seen_seeds:  # 概率极低，仅保证 key 唯一
            sub.seed_address = _fake_addr(random.Random(f"rekey:{seed_key}:{i}:{len(seen_seeds)}"))
        seen_seeds.add(sub.seed_address)
        rows.append(new_pattern_row(
            name=name, source="lazarus_synth", grade="S",
            provenance="synthetic", sub=sub))
    return rows


def run(session, count: int, seed_key: int = 42, verbose: bool = True) -> int:
    from backend.models.knowledge import Pattern

    try:
        from .common import upsert_rows
    except ImportError:  # 直接运行（python ingest/corpus_gen.py）
        from common import upsert_rows

    rows = generate(count, seed_key)
    n = upsert_rows(session, Pattern, rows, ["seed_address", "content_hash"])
    if verbose:
        print(f"synth corpus: generated={len(rows)} upserted={n}")
    return n


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

    from backend.models.knowledge import Pattern  # noqa: F401

    count = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    engine = get_engine()
    with Session(engine) as s:
        run(s, count)
