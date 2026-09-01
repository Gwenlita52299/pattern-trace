"""BFS 三队列 + 五类终止条件 — graph-builder-spec §2/§3。

终止语义与 bybit_rust 基线（btc_aml_forensics code/src/step3/step3_sub2_bfs_loop.py
process_queue_batched）逐项对齐：每个扩展单元是一次 **UTXO 消费**（基线 QueueEntry =
(txid, n, block_height, tx_index, address)），一个 UTXO 恰好计入一个主状态计数器：
    unspent / out_of_range / early_stop_wasabi / early_stop_crosschain /
    expanded / tx4_new_dst_hard_stop

层级语义沿用基线：三个 BFS 队列（layer0/layer1/layer2）的元素是 **UTXO 元组**，而不是
地址节点。每个 UTXO 条目 `(utxo_txid, utxo_n, utxo_addr)` 表示「utxo_addr 拥有 txid 的
第 utxo_n 号输出」；展开它时按 spent_by（= 谁消费了这个 UTXO）解析出消费交易，
该交易产生的 `tx{i+2}` 边把资金流向前一层。消费交易的新输出成为下一层的 UTXO 条目。

与基线的已记录分歧（均为 PatternTrace spec 决策，非疏忽）：
1. D3：tx 也建模为节点（前端可视化/evidence 高亮需要），基线中 tx 只是边属性；
2. seen_utxos 用完整三元组 (txid, vout, addr)，spec §5 禁止基线的 hash 截断防碰撞。
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from .id_contract import edge_id, node_id

# 与基线 step3_sub1_preprocessing.py 一致；≤ 阈值的输出整条丢弃
DUST_THRESHOLD_BTC = 0.0001

MAX_HOPS = 3  # D6：API 上限与 BFS 三队列一致
MAX_ROUNDS = 5  # 基线 run_bfs 默认值（BFS_MAX_ROUNDS）

# UTXO 队列条目：模式 (utxo_txid, utxo_n, utxo_addr)。
# 与基线 QueueEntry 的 (txid, n, block_height, tx_index, address) 同构，
# 但 PatternTrace 数据源用 block_time 而非 block_height/tx_index，故省略后两维。
UTXO_ENTRY = tuple[str, int, str]


def validate_hops(hops: int) -> None:
    if not isinstance(hops, int) or isinstance(hops, bool) or hops < 1 or hops > MAX_HOPS:
        raise ValueError(f"hops must be between 1 and {MAX_HOPS}")


@dataclass
class Node:
    id: str
    kind: str  # address | transaction
    label: str = ""
    balance_btc: float = 0.0
    value_btc: float = 0.0
    layer_span: int = 0
    first_layer: int = 0
    total_received_btc: float = 0.0
    total_sent_btc: float = 0.0
    utxo_count: int = 0
    direct_related_to_lazarus: bool = False


@dataclass
class Edge:
    id: str
    source: str
    target: str
    txid: str = ""
    tx_layer: str = ""  # tx2 | tx3 | tx4
    value_ratio: float = 0.0
    dst_value_btc: float = 0.0
    total_num_inputs: int = 0
    total_num_outputs: int = 0
    is_stopped_expansion: bool = False
    is_remixer: bool = False
    is_crosschain: bool = False
    op_return_protocol: str | None = None


@dataclass
class BFSStats:
    # --- 基线对齐计数器（键名与 process_queue_batched stats 一致）---
    unspent: int = 0
    out_of_range: int = 0
    early_stop_wasabi: int = 0
    early_stop_crosschain: int = 0
    expanded: int = 0
    tx4_new_dst_hard_stop: int = 0
    # --- PatternTrace 扩展（规模裁剪/降级，基线无对应物）---
    truncated_total_nodes: int = 0
    truncated_per_layer: int = 0
    fanout_folded: int = 0
    degraded: bool = False
    queue_empty: bool = True
    elapsed_ms: float = 0.0

    def termination_summary(self) -> dict[str, int]:
        """仅基线词表内的终止统计——对齐校验用。"""
        return {
            "unspent": self.unspent,
            "out_of_range": self.out_of_range,
            "early_stop_wasabi": self.early_stop_wasabi,
            "early_stop_crosschain": self.early_stop_crosschain,
            "expanded": self.expanded,
            "tx4_new_dst_hard_stop": self.tx4_new_dst_hard_stop,
        }


@dataclass
class SubgraphResult:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    stats: BFSStats = field(default_factory=BFSStats)

    def node_ids(self) -> set[str]:
        return {n.id for n in self.nodes}

    def edge_ids(self) -> set[str]:
        return {e.id for e in self.edges}

    def valid_evidence_ids(self) -> set[str]:
        return self.node_ids() | self.edge_ids()


def _within_time_window(
    tx_block_time: float | None,
    seed_block_time: float | None,
    window_days: int,
) -> bool:
    """时间窗口语义（spec §3 out_of_range 行，block_time 口径）：
    早于 seed_block_time − window 即停止扩展。缺失数据不裁剪。"""
    if tx_block_time is None or seed_block_time is None:
        return True
    window_start = seed_block_time - window_days * 86400
    return tx_block_time >= window_start


class GraphBuilder:
    def __init__(
        self,
        coinjoin_txids: set[str] | None = None,
        crosschain_tx_set: dict[str, str] | None = None,
        max_nodes_per_layer: int = 50,
        max_total_nodes: int = 200,
        fanout_truncate_threshold: int = 20,
        max_rounds: int = MAX_ROUNDS,
        dust_threshold_btc: float = DUST_THRESHOLD_BTC,
    ) -> None:
        self.coinjoin_txids = coinjoin_txids or set()
        self.crosschain_tx_set = crosschain_tx_set or {}
        self.max_nodes_per_layer = max_nodes_per_layer
        self.max_total_nodes = max_total_nodes
        self.fanout_truncate_threshold = fanout_truncate_threshold
        self.max_rounds = max_rounds
        self.dust_threshold_btc = dust_threshold_btc

    # ------------------------------------------------------------------
    # 种子 UTXO 枚举：一个地址拥有的 UTXO = 其各交易里 input 的 prevout ∪
    # 输出到自身的 UTXO（该地址的接收侧）。
    # 对应基线 read_seed_utxos 的种子行；PatternTrace 以用户地址为种子并只向前追溯
    # （资金流出方向），因此以「该地址正在消费的 prevout」为主，辅以接收侧输出。
    # ------------------------------------------------------------------
    def _owned_utxos(self, address: str, tx_provider, st: BFSStats) -> list[UTXO_ENTRY]:
        utxos: list[UTXO_ENTRY] = []
        seen: set[tuple[str, int, str]] = set()
        try:
            txs = tx_provider(address)
        except Exception:
            # 部分失败语义：种子地址数据源不可达 → 降级，不产节点
            st.degraded = True
            return utxos

        def add(txid: str, vout: int, addr: str) -> None:
            key = (txid, vout, addr)
            if key not in seen:
                seen.add(key)
                utxos.append((txid, vout, addr))

        for tx in txs:
            for idx, out_ in enumerate(getattr(tx, "outputs", None) or []):
                # 该地址作为接收方拥有的 UTXO（可能被后续花费，也可能保持 unspent）
                if out_.get("address") == address:
                    add(tx.txid, idx, address)
            for inp in getattr(tx, "inputs", None) or []:
                # prevout 指向一个该地址拥有的 UTXO（该地址在消费它）
                if inp.get("address") == address and inp.get("prev_txid"):
                    add(inp["prev_txid"], inp["prev_vout"], address)
        return utxos

    # ------------------------------------------------------------------
    # spent_by 解析：给定 UTXO (utxo_txid, utxo_n)，找出消费它的交易。
    # 消费交易必然出现在 owner 的交易列表里（owner 作为 input），故按地址扫描即可。
    # ------------------------------------------------------------------
    def _spending_tx(self, utxo_txid: str, utxo_n: int, owner: str, tx_provider, st: BFSStats):
        try:
            txs = tx_provider(owner)
        except Exception:
            st.degraded = True
            return None
        for tx in txs:
            for inp in getattr(tx, "inputs", None) or []:
                if inp.get("prev_txid") == utxo_txid and inp.get("prev_vout") == utxo_n:
                    return tx
        return None

    # ------------------------------------------------------------------
    def build(
        self,
        seed_address: str,
        tx_provider,
        hops: int = 3,
        time_window_days: int = 90,
        seed_block_time: float | None = None,
    ) -> SubgraphResult:
        validate_hops(hops)
        start = time.perf_counter()
        st = BFSStats()

        result = SubgraphResult()
        nodes_by_id: dict[str, Node] = {}
        seen_edges: set[str] = set()
        # 完整三元组 (txid, output_index, address) — spec §5 禁止 hash 截断
        seen_utxos: set[tuple[str, int, str]] = set()

        def add_node(node: Node) -> bool:
            """节点数硬上限：满则计 truncated 并拒绝。"""
            if node.id in nodes_by_id:
                return True
            if len(nodes_by_id) >= self.max_total_nodes:
                st.truncated_total_nodes += 1
                return False
            nodes_by_id[node.id] = node
            result.nodes.append(node)
            return True

        seed_id = node_id("address", seed_address)
        add_node(Node(id=seed_id, kind="address", label=seed_address))

        # 三队列：queues[i] 收 first_layer==i 的地址所拥有的 UTXO，展开时产生 tx(i+2) 边
        queues: list[deque] = [deque() for _ in range(MAX_HOPS)]
        for utxo in self._owned_utxos(seed_address, tx_provider, st):
            if utxo in seen_utxos:
                continue
            seen_utxos.add(utxo)
            queues[0].append(utxo)

        round_num = 0
        while any(queues) and round_num < self.max_rounds:
            if len(nodes_by_id) >= self.max_total_nodes:
                break
            round_num += 1
            progressed = False
            # 每轮按 L0→L1→L2 处理各队列的快照；本轮新条目留待下一轮（快照语义）
            for qi in range(min(hops, MAX_HOPS)):
                queue = queues[qi]
                snapshot = list(queue)
                queue.clear()
                # 每层共享的新 tx 节点预算（spec §8「每层共享 50」）
                layer_budget = _LayerBudget(self.max_nodes_per_layer)
                for utxo in snapshot:
                    progressed = True
                    self._process_utxo(
                        utxo, qi + 1, tx_provider, result, nodes_by_id,
                        seen_edges, seen_utxos, queues,
                        hops, time_window_days, seed_block_time, st,
                        add_node, layer_budget,
                    )
            if not progressed:
                break

        # 节点硬上限可能拒绝已生成边的端点（GB-09 场景），
        # 清理悬挂边保证 D3 引用完整性（GB-13）
        result.edges = [
            e for e in result.edges
            if e.source in nodes_by_id and e.target in nodes_by_id
        ]

        st.queue_empty = not any(queues)
        st.elapsed_ms = (time.perf_counter() - start) * 1000
        result.stats = st
        return result

    # ------------------------------------------------------------------
    def _process_utxo(
        self,
        utxo: UTXO_ENTRY,
        depth: int,            # qi+1：决定 tx_layer，对应基线 current_depth
        tx_provider,
        result: SubgraphResult,
        nodes_by_id: dict[str, Node],
        seen_edges: set[str],
        seen_utxos: set[tuple[str, int, str]],
        queues: list[deque],
        hops: int,
        time_window_days: int,
        seed_block_time: float | None,
        st: BFSStats,
        add_node,
        layer_budget: "_LayerBudget",
    ) -> None:
        """处理单个 UTXO 队列条目（≈基线的单 UTXO 扩展单元）。

        该 UTXO (utxo_txid, utxo_n) 由 utxo_addr 拥有。展开它 = 解析其消费交易
        （spent_by），消费交易即资金流向的前一跳。每个 UTXO 恰好计入一个主状态。
        """
        utxo_txid, utxo_n, utxo_addr = utxo
        tx_layer = f"tx{depth + 1}"

        # --- spent_by：谁消费了该 UTXO ---
        spending_tx = self._spending_tx(utxo_txid, utxo_n, utxo_addr, tx_provider, st)
        if spending_tx is None:
            # 无消费交易 → unspent 终止（基线 Phase 1：spent_by 查无）
            st.unspent += 1
            return

        # --- 状态判定：级联顺序与基线 Phase 1/2 一致；
        #     判定对象是「消费交易」，而非该 UTXO 本身 ---
        protocol: str | None = None
        if not _within_time_window(spending_tx.block_time, seed_block_time, time_window_days):
            outcome = "out_of_range"
        elif spending_tx.txid in self.coinjoin_txids:
            outcome = "early_stop_wasabi"
        elif spending_tx.txid in self.crosschain_tx_set:
            outcome = "early_stop_crosschain"
            protocol = self.crosschain_tx_set[spending_tx.txid]
        else:
            outcome = "expanded"

        stopped = outcome != "expanded"

        # 每层共享预算（spec §8「每层共享 50」，修正旧实现的「每队列条目 50」）：
        # 本层已创建的新 tx 节点达到上限 → 该 expanded 单元不再展开（仍计 expanded）。
        # 必须在创建 tx 节点**之前**判定，否则预算失效（GB-10 会越过上限）。
        if outcome == "expanded" and layer_budget.exceeded():
            st.truncated_per_layer += 1
            st.expanded += 1
            return

        # --- 去重优先：同一 (源地址 → 消费交易) 只创建一次边（多 UTXO 共消费同一交易时）---
        src_id = node_id("address", utxo_addr)
        tx_node_id = node_id("transaction", spending_tx.txid)
        e_id = edge_id(src_id, tx_node_id)
        if e_id not in seen_edges:
            seen_edges.add(e_id)
            inputs = getattr(spending_tx, "inputs", None) or []
            outputs_raw = spending_tx.outputs or []
            total_input = sum(i.get("value", 0) for i in inputs)
            total_output = sum(o.get("value", 0) for o in outputs_raw)
            result.edges.append(Edge(
                id=e_id, source=src_id, target=tx_node_id,
                txid=spending_tx.txid, tx_layer=tx_layer,
                total_num_inputs=len(inputs), total_num_outputs=len(outputs_raw),
                dst_value_btc=total_output,
                is_stopped_expansion=stopped,
                is_remixer=(outcome == "early_stop_wasabi"),
                is_crosschain=(outcome == "early_stop_crosschain"),
                op_return_protocol=protocol,
            ))
            if not add_node(Node(
                id=tx_node_id, kind="transaction", label=spending_tx.txid[:16],
                value_btc=total_output, first_layer=depth - 1,
            )):
                _count_outcome(st, outcome)
                return
            # 该消费交易节点纳入本层预算（去重：同一交易被多个 UTXO 消费只计一次）
            layer_budget.consume(tx_node_id, nodes_by_id)

        # 已消费但停止（unspent 已在上面处理）：out_of_range / early_stop 只计主状态，
        # 不产生向下展开的新地址分支（基线 Phase 1/2 即 return）。
        if outcome == "out_of_range":
            st.out_of_range += 1
            return
        if outcome == "early_stop_wasabi":
            st.early_stop_wasabi += 1
            return
        if outcome == "early_stop_crosschain":
            st.early_stop_crosschain += 1
            return

        # --- expanded：展开消费交易的新输出 → 下一层的 UTXO ---
        outputs_raw = spending_tx.outputs or []
        inputs = getattr(spending_tx, "inputs", None) or []
        total_input = sum(i.get("value", 0) for i in inputs)
        outputs = [
            (idx, out) for idx, out in enumerate(outputs_raw)
            if out.get("value", 0) > self.dust_threshold_btc
            and out.get("address")
        ]
        overflow = len(outputs) - self.fanout_truncate_threshold
        if overflow > 0:
            outputs = outputs[: self.fanout_truncate_threshold]

        child_fl = depth  # 基线：新地址 first_layer = 当前层 index（当前处理深度的下一层）
        can_process_child = child_fl < hops
        hard_stops = 0
        dst_edges = 0

        for idx, out in outputs:
            dst_addr = out["address"]
            child_id = node_id("address", dst_addr)
            # 子地址点：若已存在则只补边，不重复建节点（如自转账回到种子）。
            # 注意：即使边去重（同一 tx→addr D3 边已建），该输出仍是**一个独立的
            # UTXO**（不同 output_index），必须照常入队到下一层（GB-07 完整三元组）。
            ce = edge_id(tx_node_id, child_id)
            new_addr = child_id not in nodes_by_id
            dst_value = out.get("value", 0)
            if ce not in seen_edges:
                seen_edges.add(ce)
                result.edges.append(Edge(
                    id=ce, source=tx_node_id, target=child_id,
                    txid=spending_tx.txid, tx_layer=tx_layer,
                    value_ratio=dst_value / total_input if total_input > 0 else 0.0,
                    dst_value_btc=dst_value,
                    total_num_inputs=len(inputs), total_num_outputs=len(outputs_raw),
                ))
                if new_addr:
                    if not add_node(Node(
                        id=child_id, kind="address", label=dst_addr,
                        first_layer=child_fl,
                        total_received_btc=dst_value, utxo_count=1,
                        layer_span=child_fl - (depth - 1),
                    )):
                        continue
            elif new_addr:
                # 边已存在但节点尚未建（前一轮 add_node 拒绝导致悬挂）——此处补建
                if not add_node(Node(
                    id=child_id, kind="address", label=dst_addr,
                    first_layer=child_fl,
                    total_received_btc=dst_value, utxo_count=1,
                    layer_span=child_fl - (depth - 1),
                )):
                    continue
            dst_edges += 1

            if can_process_child:
                # 新 UTXO 三元组去重（防同一 UTXO 被重复入队，GB-07 完整三元组）
                utxo_key = (spending_tx.txid, idx, dst_addr)
                if utxo_key not in seen_utxos:
                    seen_utxos.add(utxo_key)
                    queues[child_fl].append((spending_tx.txid, idx, dst_addr))
            elif new_addr:
                # 基线：最终层的新目标地址只记边不入队（tx4 hard stop）
                hard_stops += 1

        # fanout 折叠：超出阈值的输出聚合为摘要节点（计入总节点数硬上限）
        if overflow > 0:
            summary_id = f"tx:{spending_tx.txid}:overflow"
            if add_node(Node(
                id=summary_id, kind="transaction",
                label=f"{overflow} more outputs...",
            )):
                se = edge_id(tx_node_id, summary_id)
                if se not in seen_edges:
                    seen_edges.add(se)
                    result.edges.append(Edge(id=se, source=tx_node_id, target=summary_id))
                st.fanout_folded += overflow

        # 主状态计数：基线规则「全部边均为 tx4 新址硬停止 → 记 tx4，否则 expanded」
        if not can_process_child and dst_edges > 0 and dst_edges == hard_stops:
            st.tx4_new_dst_hard_stop += 1
        else:
            st.expanded += 1


class _LayerBudget:
    """每层共享的新 tx 节点预算（spec §8「每层共享 50」，修正旧实现的「每队列条目 50」）。"""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.created_new: set[str] = set()

    def consume(self, tx_node_id: str, nodes_by_id: dict[str, Node]) -> None:
        if tx_node_id not in nodes_by_id:
            return
        if tx_node_id not in self.created_new:
            self.created_new.add(tx_node_id)

    def exceeded(self) -> bool:
        return self.limit >= 0 and len(self.created_new) >= self.limit


def _count_outcome(st: BFSStats, outcome: str) -> None:
    """边界：add_node 拒绝消费交易节点时，主状态仍计入该 UTXO 的终止。"""
    if outcome == "out_of_range":
        st.out_of_range += 1
    elif outcome == "early_stop_wasabi":
        st.early_stop_wasabi += 1
    elif outcome == "early_stop_crosschain":
        st.early_stop_crosschain += 1
    elif outcome == "expanded":
        st.expanded += 1
