"""BFS 三队列 + 五类终止条件 — graph-builder-spec §2/§3。

终止语义与参考实现基线（btc_aml_forensics code/src/step3/step3_sub2_bfs_loop.py
process_queue_batched）逐项对齐：每个扩展单元是一次 **UTXO 消费**（基线 QueueEntry =
(txid, n, block_height, tx_index, address)），一个 UTXO 恰好计入一个主状态计数器：
    unspent / out_of_range / early_stop_wasabi / early_stop_crosschain /
    expanded / tx4_new_dst_hard_stop

层级语义沿用基线：三个 BFS 队列（layer0/layer1/layer2）的元素是 **UTXO 元组**，而不是
地址节点。每个 UTXO 条目 `(utxo_txid, utxo_n, utxo_addr)` 表示「utxo_addr 拥有 txid 的
第 utxo_n 号输出」；展开它时按 spent_by（= 谁消费了这个 UTXO）解析出消费交易，
该交易产生的 `tx{i+2}` 边把资金流向前一层。消费交易的新输出成为下一层的 UTXO 条目。

spent_by 解析在 issue #3 后采用 Esplora outspend API（GET /tx/:txid/outspend/:vout）
作为权威来源（见 _spending_tx_outspend）；provider 未实现该协议时回退到地址扫描。

与基线的已记录分歧（均为 PatternTrace spec 决策，非疏忽）：
1. D3：tx 也建模为节点（前端可视化/evidence 高亮需要），基线中 tx 只是边属性；
2. seen_utxos 用完整三元组 (txid, vout, addr)，spec §5 禁止基线的 hash 截断防碰撞。
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from .id_contract import edge_id, node_id
from ..detection.coinjoin import CoinJoinDetector
from ..detection.crosschain import CrosschainDetector

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
    # 与基线 step3_subgraph 节点表对齐的三级标签（canonical schema 一致性）：
    # confirmed/probably 是 Lazarus 归因硬真值，检索通道严禁读取（防泄漏），
    # 仅用于评估与真值判定；is_censored 参与规模统计特征
    confirmed_downstream: bool = False
    probably_lazarus_related: bool = False
    is_censored: bool = False


@dataclass
class Edge:
    id: str
    source: str
    target: str
    txid: str = ""
    tx_layer: str = ""  # tx2 | tx3 | tx4
    value_ratio: float = 0.0
    # 源侧金额（addr→tx 边）：源地址在该交易中消费的 UTXO 金额（同地址多
    # UTXO 共消费同一交易时为累加值）。tx→addr 边不携带（源侧是交易本身）；
    # tx1 种子边携带 total_input（种子交易的输入总额）。前端兄弟节点排序按
    # 此字段降序（「出金额」），缺失时回退 dst_value_btc。
    src_value_btc: float = 0.0
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
    # issue #8：部分失败保留 + 数据质量元数据
    missing_branches: int = 0
    source_errors: list[dict] = field(default_factory=list)

    @property
    def data_quality(self) -> str:
        """degraded：存在上游请求失败（部分子图不完整）；否则 complete。"""
        return "degraded" if self.degraded else "complete"

    @property
    def requires_manual_review(self) -> bool:
        return self.degraded

    def record_error(self, *, stage: str, address: str | None,
                     error_code: str, message: str = "") -> None:
        """记录一次上游分支失败：置 degraded、累加缺失分支数、留源错误摘要。"""
        self.degraded = True
        self.missing_branches += 1
        self.source_errors.append({
            "stage": stage,
            "address": address,
            "error_code": error_code,
            "message": message,
        })

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


def _esplora_error_code(exc: BaseException | None) -> str:
    """把上游图数据异常映射为错误码；无异常上下文返回通用 UPSTREAM_ERROR。"""
    if exc is None:
        return "UPSTREAM_ERROR"
    from .data_source import BudgetExhaustedError
    if isinstance(exc, BudgetExhaustedError):
        return BudgetExhaustedError.error_code
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "TIMEOUT"
    try:
        import httpx
    except ImportError:  # pragma: no cover
        httpx = None
    if httpx is not None and isinstance(exc, httpx.HTTPStatusError) \
            and exc.response.status_code == 429:
        return "RATE_LIMITED"
    if httpx is not None and isinstance(exc, httpx.HTTPError):
        return "HTTP_ERROR"
    return "UPSTREAM_ERROR"


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
        max_nodes_per_layer: int = 50,
        max_total_nodes: int = 200,
        fanout_truncate_threshold: int = 20,
        max_rounds: int = MAX_ROUNDS,
        dust_threshold_btc: float = DUST_THRESHOLD_BTC,
        coinjoin_detector: CoinJoinDetector | None = None,
        crosschain_detector: CrosschainDetector | None = None,
        build_time_budget_seconds: float | None = None,
    ) -> None:
        self.coinjoin_txids = coinjoin_txids or set()
        # 结构级启发式判定：任一消费交易不在 coinjoin_txids 集合里时，
        # 直接按交易结构规则判别是否 CoinJoin（无 CSV / ML 依赖）。
        self.coinjoin_detector = coinjoin_detector or CoinJoinDetector()
        # 运行时跨链检测（issue #4/#5）：GraphBuilder 只依赖稳定的 Detector 接口，
        # 判定完全来自 Esplora 交易字段，不再依赖 crosschain_tx_set / CSV 标签库。
        self.crosschain_detector = crosschain_detector or CrosschainDetector()
        self.max_nodes_per_layer = max_nodes_per_layer
        self.max_total_nodes = max_total_nodes
        self.fanout_truncate_threshold = fanout_truncate_threshold
        self.max_rounds = max_rounds
        self.dust_threshold_btc = dust_threshold_btc
        # issue #80：时长预算（builder 侧计时）；None = 由调用方传 settings 值
        self.build_time_budget_seconds = build_time_budget_seconds

    # ------------------------------------------------------------------
    # 种子 UTXO 枚举（issue #3 地址模式）：一个地址拥有的 UTXO 只来自**输出侧**——
    # 该地址作为接收方（vout 的 scriptpubkey_address == address）被创建的新 UTXO。
    # 对应基线 read_seed_utxos 的种子行；只向前追溯（资金流出方向）。
    # 明确区分：vout 出现地址 = 该交易为此地址创建了新 UTXO（种子来源）；
    #           vin 出现地址 = 该地址在消费旧 UTXO（不作为种子来源）。
    # ------------------------------------------------------------------
    def _owned_utxos(self, address: str, tx_provider, st: BFSStats) -> list[UTXO_ENTRY]:
        utxos: list[UTXO_ENTRY] = []
        seen: set[tuple[str, int, str]] = set()
        try:
            txs = self._address_txs(tx_provider, address, st)
        except Exception as exc:
            # 部分失败语义：种子地址数据源不可达 → 降级，不产节点
            st.record_error(stage="esplora", address=address,
                            error_code=_esplora_error_code(exc), message=str(exc))
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
        return utxos

    def _address_txs(self, tx_provider, address: str, st) -> list:
        """地址交易枚举：优先 provider.address_txs（live 全量分页 / fixture），
        否则回退到旧的 callable(address) 形态（纯测试桩/基准脚本）。

        issue #25：live provider 因页数上限截断历史时显式置 degraded——
        子图基于不完整历史构建，不得伪装成 complete。
        """
        if hasattr(tx_provider, "address_txs"):
            txs = tx_provider.address_txs(address)
        else:
            txs = tx_provider(address)
        truncated = getattr(tx_provider, "truncated_addresses", None)
        if truncated and address in truncated:
            st.record_error(stage="esplora", address=address,
                            error_code="HISTORY_TRUNCATED",
                            message=truncated[address])
        return txs

    # ------------------------------------------------------------------
    # spent_by 解析：给定 UTXO (utxo_txid, utxo_n)，找出消费它的交易。
    # issue #3：以 Esplora outspend API（GET /tx/:txid/outspend/:vout）作为权威来源，
    # 修复「扫描地址交易列表可能遗漏历史消费交易 / 把已花 UTXO 误判为 unspent」。
    # 仅当 provider 未实现 outspend 协议（纯 callable 测试桩）时回退到地址扫描。
    # ------------------------------------------------------------------
    def _spending_tx(self, utxo_txid: str, utxo_n: int, owner: str, tx_provider, st: BFSStats):
        # issue #3：优先领域级 resolve_spending_transaction（封装 outspend+get_tx），
        # 其次退回 outspend+get_tx 组合（纯测试桩），再退回地址扫描（callable 桩）。
        if hasattr(tx_provider, "resolve_spending_transaction"):
            return self._spending_tx_domain(utxo_txid, utxo_n, owner, tx_provider, st)
        if hasattr(tx_provider, "outspend"):
            return self._spending_tx_outspend(utxo_txid, utxo_n, owner, tx_provider, st)
        return self._spending_tx_scan(utxo_txid, utxo_n, owner, tx_provider, st)

    def _spending_tx_domain(self, utxo_txid: str, utxo_n: int, owner: str,
                            tx_provider, st: BFSStats):
        """用领域级 resolve_spending_transaction 解析消费交易（issue #3）。"""
        try:
            res = tx_provider.resolve_spending_transaction(utxo_txid, utxo_n, owner=owner)
        except Exception as exc:
            st.record_error(stage="esplora", address=owner,
                            error_code=_esplora_error_code(exc), message=str(exc))
            return None
        if getattr(res, "spent", False) is not True or not getattr(res, "spending_txid", None):
            return None  # 权威未花 → unspent 叶子（unspent 终止，不产边）
        spending_tx = getattr(res, "spending_tx", None)
        if spending_tx is not None:
            return spending_tx
        # 领域对象未附完整交易（extremely 少见）→ 补拉全量，稳健兜底
        if hasattr(tx_provider, "get_tx"):
            try:
                tx = tx_provider.get_tx(res.spending_txid)
            except Exception as exc:
                st.record_error(stage="esplora", address=owner,
                                error_code=_esplora_error_code(exc), message=str(exc))
                return None
            if tx is None:
                st.record_error(stage="esplora", address=owner,
                                error_code="UPSTREAM_ERROR",
                                message="get_tx returned None")
                return None
            return tx
        st.record_error(stage="esplora", address=owner,
                        error_code="UPSTREAM_ERROR",
                        message="provider missing get_tx")
        return None

    def _spending_tx_outspend(self, utxo_txid: str, utxo_n: int, owner: str,
                              tx_provider, st: BFSStats):
        """用 outspend 权威判定该 UTXO 是否被消费及其消费交易，再拉全量交易。"""
        try:
            info = tx_provider.outspend(utxo_txid, utxo_n)
        except Exception as exc:
            st.record_error(stage="esplora", address=owner,
                            error_code=_esplora_error_code(exc), message=str(exc))
            return None
        if getattr(info, "spent", False) is not True or not getattr(info, "txid", None):
            return None  # 权威未花 → unspent 叶子（unspent 终止，不产边）
        spending_txid = info.txid

        # 完整消费交易：优先 get_tx；否则回退到地址扫描按 txid 匹配（极少见，仅无 get_tx 的桩）
        if hasattr(tx_provider, "get_tx"):
            try:
                tx = tx_provider.get_tx(spending_txid)
            except Exception as exc:
                st.record_error(stage="esplora", address=owner,
                                error_code=_esplora_error_code(exc), message=str(exc))
                return None
            if tx is None:
                st.record_error(stage="esplora", address=owner,
                                error_code="UPSTREAM_ERROR",
                                message="get_tx returned None")
                return None
            return tx

        try:
            for tx in tx_provider(owner):
                if getattr(tx, "txid", None) == spending_txid:
                    return tx
        except Exception as exc:
            st.record_error(stage="esplora", address=owner,
                            error_code=_esplora_error_code(exc), message=str(exc))
            return None
        st.record_error(stage="esplora", address=owner,
                        error_code="UPSTREAM_ERROR",
                        message="spending transaction not found")
        return None

    def _spending_tx_scan(self, utxo_txid: str, utxo_n: int, owner: str,
                          tx_provider, st: BFSStats):
        """旧行为：按地址扫描其交易列表，匹配引用该 UTXO 的 input（仅为兼容保留）。"""
        try:
            txs = tx_provider(owner)
        except Exception as exc:
            st.record_error(stage="esplora", address=owner,
                            error_code=_esplora_error_code(exc), message=str(exc))
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
        """地址种子入口：以 seed_address 拥有的 UTXO 为根队列。

        与 build_from_txid 共享同一套 outspend 权威展开核心（_run_bfs）。
        """
        validate_hops(hops)
        st = BFSStats()
        start = time.perf_counter()
        seed_utxos = self._owned_utxos(seed_address, tx_provider, st)
        seed_nodes = [Node(id=node_id("address", seed_address), kind="address", label=seed_address)]
        return self._run_bfs(
            seed_utxos, seed_nodes, [], tx_provider,
            hops, time_window_days, seed_block_time, st, start,
        )

    # ------------------------------------------------------------------
    # issue #3「输入为 txid」：一个交易可形成多个子图（每个输出 UTXO 是一个根分支）。
    #   GET /tx/:txid → 取 vout[] → 排除 OP_RETURN 等不可追踪输出 → 每个输出为根 UTXO。
    # ------------------------------------------------------------------
    def build_from_txid(
        self,
        seed_txid: str,
        tx_provider,
        hops: int = 3,
        time_window_days: int = 90,
        seed_block_time: float | None = None,
    ) -> SubgraphResult:
        """交易种子入口：把 seed_txid 的可追踪输出 UTXO 作为根队列，按 outspend 展开。"""
        validate_hops(hops)
        st = BFSStats()
        start = time.perf_counter()
        seed_utxos, seed_nodes, seed_edges = self._owned_utxos_from_tx(seed_txid, tx_provider, st)
        return self._run_bfs(
            seed_utxos, seed_nodes, seed_edges, tx_provider,
            hops, time_window_days, seed_block_time, st, start,
        )

    def _owned_utxos_from_tx(
        self,
        seed_txid: str,
        tx_provider,
        st: BFSStats,
    ) -> tuple[list[UTXO_ENTRY], list[Node], list[Edge]]:
        """从 seed_txid 的输出侧枚举可追踪根 UTXO（issue #3「输入为 txid」）。

        - `GET /tx/:txid` 取交易详情（provider.get_tx）
        - 只保留 vout 中带可追踪地址的输出（排除 OP_RETURN 等无地址/不可追踪输出与 dust）
        - 每个匹配输出 `(seed_txid, idx, addr)` 是一个根 UTXO 分支
        - 同时产出 T0 → addr 支付边，保证 D3 引用完整性（种子交易连到根输出所有者）
        """
        if not hasattr(tx_provider, "get_tx"):
            st.record_error(stage="esplora", address=seed_txid,
                            error_code="UPSTREAM_ERROR",
                            message="provider missing get_tx")
            return [], [], []
        try:
            tx = tx_provider.get_tx(seed_txid)
        except Exception as exc:
            st.record_error(stage="esplora", address=seed_txid,
                            error_code=_esplora_error_code(exc), message=str(exc))
            return [], [], []
        if tx is None:
            st.record_error(stage="esplora", address=seed_txid,
                            error_code="UPSTREAM_ERROR",
                            message="get_tx returned None")
            return [], [], []

        seed_node = Node(id=node_id("transaction", seed_txid),
                        kind="transaction", label=seed_txid[:16])
        seed_nodes: list[Node] = [seed_node]
        seed_edges: list[Edge] = []
        utxos: list[UTXO_ENTRY] = []
        seen: set[tuple[str, int, str]] = set()
        inputs = getattr(tx, "inputs", None) or []
        outputs = getattr(tx, "outputs", None) or []
        total_input = sum(i.get("value", 0) for i in inputs)

        for idx, out_ in enumerate(outputs):
            addr = out_.get("address")
            value = out_.get("value", 0)
            # 排除 OP_RETURN / 无地址输出（不可追踪）与 dust（≤ 阈值整条丢弃）
            if not addr or value <= self.dust_threshold_btc:
                continue
            key = (seed_txid, idx, addr)
            if key in seen:
                continue
            seen.add(key)
            utxos.append((seed_txid, idx, addr))
            addr_id = node_id("address", addr)
            seed_nodes.append(Node(id=addr_id, kind="address", label=addr,
                                   first_layer=0, total_received_btc=value, utxo_count=1))
            seed_edges.append(Edge(
                id=edge_id(seed_node.id, addr_id),
                source=seed_node.id, target=addr_id,
                txid=seed_txid, tx_layer="tx1",
                value_ratio=value / total_input if total_input > 0 else 0.0,
                src_value_btc=total_input,
                dst_value_btc=value,
                total_num_inputs=len(inputs), total_num_outputs=len(outputs),
            ))
        return utxos, seed_nodes, seed_edges

    # ------------------------------------------------------------------
    def _budget_stop_reason(self, tx_provider, start: float) -> str | None:
        """issue #80：BFS 主循环每轮检查建图预算，返回终止原因（None = 继续）。

        请求预算由 provider 在 _fetch 处维护（缓存命中不计）；时长预算
        builder 侧独立计时（覆盖 provider 未带预算元数据的测试桩形态）。
        """
        exhausted = getattr(tx_provider, "budget_exhausted", None)
        if exhausted is True:
            return str(getattr(tx_provider, "budget_exhausted_reason", "")
                       or "request budget exhausted")
        budget_seconds = self.build_time_budget_seconds
        if budget_seconds and budget_seconds > 0 \
                and time.perf_counter() - start >= budget_seconds:
            return f"build time budget exhausted: >= {budget_seconds}s"
        return None

    def _run_bfs(
        self,
        seed_utxos: list[UTXO_ENTRY],
        seed_nodes: list[Node],
        seed_edges: list[Edge],
        tx_provider,
        hops: int,
        time_window_days: int,
        seed_block_time: float | None,
        st: BFSStats,
        start: float,
    ) -> SubgraphResult:
        """共享 BFS 核心：种子设置（节点/边/根队列）→ 三队列展开 → 清理与统计。

        地址种子与交易种子共用。st/start 由调用方预置，以便把根枚举耗时计入 elapsed。
        """
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

        # 种子节点（地址种子：地址节点；交易种子：种子交易节点 + 各根输出所有者节点）
        for node in seed_nodes:
            add_node(node)

        # 种子边（交易种子：T0 → 各根输出所有者，保证 D3 引用完整性）
        for e in seed_edges:
            if e.id in seen_edges:
                continue
            if e.source not in nodes_by_id or e.target not in nodes_by_id:
                continue  # 节点硬上限拒绝端点时跳过该边
            seen_edges.add(e.id)
            result.edges.append(e)

        # 三队列：queues[i] 收 first_layer==i 的地址所拥有的 UTXO，展开时产生 tx(i+2) 边
        queues: list[deque] = [deque() for _ in range(MAX_HOPS)]
        for utxo in seed_utxos:
            if utxo in seen_utxos:
                continue
            seen_utxos.add(utxo)
            queues[0].append(utxo)

        round_num = 0
        while any(queues) and round_num < self.max_rounds:
            if len(nodes_by_id) >= self.max_total_nodes:
                break
            # issue #80：请求/时长预算耗尽即提前收敛——清空队列、置 degraded，
            # 不再逐分支触发 provider 抛错（那只会多走一轮快照循环）
            budget_reason = self._budget_stop_reason(tx_provider, start)
            if budget_reason:
                st.record_error(stage="esplora", address=None,
                                error_code="BUDGET_EXHAUSTED",
                                message=budget_reason)
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
        elif (spending_tx.txid in self.coinjoin_txids
              or self.coinjoin_detector.is_coinjoin(spending_tx)):
            outcome = "early_stop_wasabi"
        elif (xdet := self.crosschain_detector.detect(spending_tx)).is_crosschain:
            # issue #4/#5：运行时 OP_RETURN / pegout 检测确认跨链，主判定仅此来源，
            # 不新增协议 if/else，也不再回退到 crosschain_tx_set / CSV 标签库。
            outcome = "early_stop_crosschain"
            protocol = xdet.protocol
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
        inputs = getattr(spending_tx, "inputs", None) or []
        outputs_raw = spending_tx.outputs or []
        total_input = sum(i.get("value", 0) for i in inputs)
        total_output = sum(o.get("value", 0) for o in outputs_raw)
        # 该 UTXO 的消费金额（按 prev_txid/prev_vout 匹配交易输入）
        utxo_value = next(
            (i.get("value", 0) for i in inputs
             if i.get("prev_txid") == utxo_txid and i.get("prev_vout") == utxo_n),
            0)
        if e_id not in seen_edges:
            seen_edges.add(e_id)
            result.edges.append(Edge(
                id=e_id, source=src_id, target=tx_node_id,
                txid=spending_tx.txid, tx_layer=tx_layer,
                total_num_inputs=len(inputs), total_num_outputs=len(outputs_raw),
                src_value_btc=utxo_value,
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
        else:
            # 同地址多 UTXO 共消费同一笔交易（归集/扫币场景）：去重边已存在，
            # 但本 UTXO 的消费额不能丢——累加到该边的 src_value_btc 上
            dup = next((x for x in result.edges if x.id == e_id), None)
            if dup:
                dup.src_value_btc += utxo_value

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
