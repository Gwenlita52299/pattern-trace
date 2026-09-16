"""benchmark 对齐通道与子图结构单测 — 对应 last_version/graphormer_test
benchmark_unseen_sim.py（业务级 unseen-similarity leave-one-out 基准）。

覆盖：
- 通道属性：同构 1.0、金额尺度不变、剪叶稳健、方向感知、真值标签防泄漏
- fp 指纹：恒等、档位、计数向量
- ov：精确重叠数学
- ingest 子图结构：三级标签字段、闭包 MAX_DEPTH/NODE_CAP 上限
- Retriever 精排组合：mini leave-one-out 业务场景 + 权重契约
"""
import pytest

from backend.retrieval.channels import fp_channel, ov_channel, wl_channel
from ingest.common import CLOSURE_MAX_DEPTH, CLOSURE_NODE_CAP, _node_dict, slice_by_seed


# ---------------------------------------------------------------------------
# canonical 图构造（与 ingest canonical schema 同构）
# ---------------------------------------------------------------------------
def _addr(a, layer=0, recv=1.0, sent=0.9, utxo=3, **flags):
    return {"id": f"addr:{a}", "kind": "address", "label": a,
            "first_layer": layer, "total_received_btc": recv,
            "total_sent_btc": sent, "utxo_count": utxo,
            "direct_related_to_lazarus": False, "confirmed_downstream": False,
            "probably_lazarus_related": False, "is_censored": False, **flags}


def _tx_node(txid):
    return {"id": f"tx:{txid}", "kind": "transaction",
            "label": txid[:16], "first_layer": 0}


def _spend(src, dst, txid, *, value=0.5, layer=1, stopped=False, **kw):
    """一笔交易：addr→tx 与 tx→addr（stopped 时无后者）。"""
    base = {"txid": txid, "tx_layer": f"tx{layer + 2}",
            "total_num_inputs": 2, "total_num_outputs": 3,
            "is_stopped_expansion": stopped, "is_remixer": False,
            "is_crosschain": False, "op_return_protocol": None}
    base.update(kw)
    pair = [{"id": f"edge:{src}->{txid}", "source": f"addr:{src}",
             "target": f"tx:{txid}", **base, "dst_value_btc": value}]
    if not stopped:
        pair.append({"id": f"edge:{txid}->{dst}", "source": f"tx:{txid}",
                     "target": f"addr:{dst}", **base, "dst_value_btc": value})
    return pair


def _chain_canon(n, *, value=0.5, seed="s"):
    """地址链 s → a1 → … → a{n-1}（每跳一笔交易）。"""
    nodes = [_addr(seed, layer=0)] + [_addr(f"a{i}", layer=i) for i in range(1, n)]
    edges = []
    for i in range(n - 1):
        src = seed if i == 0 else f"a{i}"
        edges += _spend(src, f"a{i + 1}", f"t{i}", value=value, layer=i)
    txs = [_tx_node(f"t{i}") for i in range(n - 1)]
    return {"nodes": nodes + txs, "edges": edges,
            "stats": {"node_count": len(nodes) + len(txs)}}


def _star_canon(n, *, seed="c"):
    nodes = [_addr(seed, layer=0)]
    edges = []
    for i in range(n - 1):
        edges += _spend(seed, f"l{i}", f"t{i}", value=0.2)
        nodes.append(_addr(f"l{i}", layer=1))
    txs = [_tx_node(f"t{i}") for i in range(n - 1)]
    return {"nodes": nodes + txs, "edges": edges}


def _prune_leaves(canon, frac=0.3):
    """剪掉 frac 比例的叶子地址（连同其 tx），模拟 leave-one-out 查询构造。"""
    addr_nodes = [n for n in canon["nodes"] if n["kind"] == "address"]
    out_srcs = {e["source"] for e in canon["edges"]
                if str(e["source"]).startswith("addr:")}
    seed_id = next(n["id"] for n in addr_nodes if n["first_layer"] == 0)
    leaf_ids = [n["id"] for n in addr_nodes
                if n["id"] != seed_id and n["id"] not in out_srcs]
    drop_ids = set(leaf_ids[:max(1, int(len(leaf_ids) * frac))])
    # 被删地址的 tx：tx→addr 边的 target 即地址节点 id
    tx_of_dst = {e["target"]: e["txid"] for e in canon["edges"]
                 if str(e["source"]).startswith("tx:")}
    drop_txids = {tx_of_dst[a] for a in drop_ids if a in tx_of_dst}
    nodes = [n for n in canon["nodes"]
             if n["id"] not in drop_ids
             and not (n["kind"] == "transaction"
                      and n["id"].split(":", 1)[1] in drop_txids)]
    edges = [e for e in canon["edges"]
             if e["target"] not in drop_ids and e["txid"] not in drop_txids]
    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# wljac 通道 — 基准的核心结构信号
# ---------------------------------------------------------------------------
class TestWlChannel:
    def test_isomorphic_exact_score(self):
        """同构副本（不同地址、相同结构/L/U/D 档）→ 分数 1.0。"""
        a = _chain_canon(5)
        b = _chain_canon(5, seed="x_s")
        assert wl_channel(a, b) == pytest.approx(1.0)

    def test_scale_invariance(self):
        """±15% 金额扰动（基准查询扰动）不得改变 wl 分——标签无金额字段。"""
        a = _chain_canon(6)
        scaled = _chain_canon(6, value=0.5 * 1.15)
        assert wl_channel(a, scaled) == pytest.approx(1.0)

    def test_pruned_clone_stays_similar(self):
        """leave-one-out 场景：剪叶 ~30% 的克隆对原版仍保持显著结构相似。"""
        full = _chain_canon(10)
        pruned = _prune_leaves(full, frac=0.3)
        assert len(pruned["nodes"]) < len(full["nodes"])
        assert wl_channel(pruned, full) > 0.5

    def test_direction_awareness(self):
        """方向反转必须降分（基准 v2 方向感知是主信号来源）。"""
        g = _chain_canon(5)
        reversed_g = {"nodes": list(g["nodes"]),
                      "edges": [{**e, "id": "r" + e["id"],
                                 "source": e["target"], "target": e["source"]}
                                for e in g["edges"]]}
        rev = wl_channel(g, reversed_g)
        assert rev < 0.95

    def test_chain_vs_star_low(self):
        assert wl_channel(_chain_canon(5), _star_canon(5)) < 0.3

    def test_attribute_difference_detected(self):
        """U 档（UTXO 数量级）差异被标签捕捉。"""
        a = _chain_canon(5)
        b = _chain_canon(5, seed="s")
        b["nodes"] = [{**n, "utxo_count": 1000} for n in b["nodes"]]
        assert wl_channel(a, b) < 1.0

    def test_empty_boundaries(self):
        empty = {"nodes": [], "edges": []}
        assert wl_channel(empty, empty) == 1.0
        assert wl_channel(empty, _chain_canon(3)) == 0.0
        assert wl_channel(_chain_canon(3), empty) == 0.0


# ---------------------------------------------------------------------------
# fp 通道
# ---------------------------------------------------------------------------
class TestFpChannel:
    def test_identical_graphs_score_one(self):
        g = _chain_canon(6)
        assert fp_channel(g, _chain_canon(6)) == pytest.approx(1.0)

    def test_amount_bucketing_tolerance(self):
        """±15% 金额扰动落在同一 log 档 → fp 分接近满分。"""
        a = _chain_canon(6, value=0.5)
        b = _chain_canon(6, value=0.5 * 1.15)
        assert fp_channel(a, b) > 0.95

    def test_10x_amount_shifts_bucket(self):
        a = _chain_canon(6, value=0.5)
        b = _chain_canon(6, value=5.0)
        assert fp_channel(a, b) < 1.0

    def test_count_vector_counts_edges_and_nodes(self):
        small, big = _chain_canon(3), _chain_canon(9)
        assert fp_channel(small, big) < 1.0


# ---------------------------------------------------------------------------
# ov 通道 — 同源案例的业务级证据
# ---------------------------------------------------------------------------
class TestOvChannel:
    def test_exact_overlap_math(self):
        q = {"nodes": [_addr("a"), _addr("b"), _addr("c")], "edges": []}
        c = {"nodes": [_addr("a"), _addr("b"), _addr("x"), _addr("y")],
             "edges": []}
        # |q∩c|=2, min(|q|,|c|)=3 → 2/3
        assert ov_channel(q, c) == pytest.approx(2 / 3)

    def test_pruned_query_full_containment(self):
        """查询是候选的剪叶版 → q ⊂ c → ov=1（软包含对剪叶完全稳定）。"""
        full = _chain_canon(10)
        pruned = _prune_leaves(full, frac=0.3)
        assert ov_channel(pruned, full) == pytest.approx(1.0)

    def test_disjoint_zero(self):
        a = {"nodes": [_addr("a1"), _addr("a2")], "edges": []}
        b = {"nodes": [_addr("b1"), _addr("b2")], "edges": []}
        assert ov_channel(a, b) == 0.0

    def test_empty_both_one(self):
        assert ov_channel({"nodes": [], "edges": []},
                          {"nodes": [], "edges": []}) == 1.0


# ---------------------------------------------------------------------------
# 防泄漏 — 真值标签不得进入任何通道
# ---------------------------------------------------------------------------
class TestLeakageGuard:
    def test_ground_truth_flags_do_not_affect_channels(self):
        """confirmed/probably/is_censored 翻转后三通道分数必须完全不变。"""
        base = _chain_canon(6)
        flagged = _chain_canon(6, seed="s")
        flagged["nodes"] = [
            {**n, "confirmed_downstream": True,
             "probably_lazarus_related": True, "is_censored": True}
            for n in flagged["nodes"]]
        other = _chain_canon(8)
        for chan in (wl_channel, fp_channel, ov_channel):
            assert chan(base, other) == pytest.approx(chan(flagged, other))


# ---------------------------------------------------------------------------
# ingest 子图结构 — 三级标签 + 闭包上限（与基准数据结构一致）
# ---------------------------------------------------------------------------
class TestNodeSchemaAlignment:
    def test_node_dict_carries_label_flags(self):
        row = {"address": "bc1qtest", "first_layer": 1,
               "total_received_btc": 2.0, "total_sent_btc": 1.0,
               "utxo_count": 4, "direct_related_to_lazarus": 1,
               "confirmed_downstream": 1, "probably_lazarus_related": 0,
               "is_censored": 1}
        n = _node_dict(row)
        assert n["confirmed_downstream"] is True
        assert n["probably_lazarus_related"] is False
        assert n["is_censored"] is True
        assert n["direct_related_to_lazarus"] is True

    def test_node_dict_defaults_false_when_columns_missing(self):
        row = {"address": "bc1qtest", "first_layer": 0,
               "total_received_btc": 1.0, "total_sent_btc": 0.9,
               "utxo_count": 1}
        n = _node_dict(row)
        assert n["confirmed_downstream"] is False
        assert n["probably_lazarus_related"] is False
        assert n["is_censored"] is False


class TestClosureCaps:
    def _rows_chain(self, n_layers):
        nodes = [{"address": "S", "first_layer": 0,
                  "total_received_btc": 1.0, "total_sent_btc": 0.9,
                  "utxo_count": 1, "direct_related_to_lazarus": 0}]
        for i in range(1, n_layers + 1):
            nodes.append({"address": f"a{i}", "first_layer": i,
                          "total_received_btc": 0.1, "total_sent_btc": 0.0,
                          "utxo_count": 1, "direct_related_to_lazarus": 0})
        edges = [{"src_address": "S" if i == 0 else f"a{i}",
                  "dst_address": f"a{i + 1}", "txid": f"t{i}",
                  "tx_layer": "tx2", "is_stopped_expansion": False,
                  "is_remixer": False, "is_crosschain": False,
                  "op_return_protocol": None}
                 for i in range(n_layers)]
        return nodes, edges

    def test_depth_capped_at_four(self):
        nodes, edges = self._rows_chain(8)
        sub = slice_by_seed(nodes, edges)[0]
        layers = {n["first_layer"] for n in sub.nodes}
        assert max(layers) <= CLOSURE_MAX_DEPTH == 4

    def test_node_cap_at_200(self):
        # 宽星型：seed + 250 个 layer1 叶子 → 地址节点数封顶 200
        nodes = [{"address": "S", "first_layer": 0,
                  "total_received_btc": 1.0, "total_sent_btc": 0.9,
                  "utxo_count": 1, "direct_related_to_lazarus": 0}]
        edges = []
        for i in range(250):
            nodes.append({"address": f"l{i}", "first_layer": 1,
                          "total_received_btc": 0.1, "total_sent_btc": 0.0,
                          "utxo_count": 1, "direct_related_to_lazarus": 0})
            edges.append({"src_address": "S", "dst_address": f"l{i}",
                          "txid": f"t{i}", "tx_layer": "tx2",
                          "is_stopped_expansion": False, "is_remixer": False,
                          "is_crosschain": False, "op_return_protocol": None})
        sub = slice_by_seed(nodes, edges)[0]
        # cap 约束「真实闭包成员」：截断产生的 dst 以 first_layer=-1 占位节点
        # 保持引用完整（基准中为 dangling 边，无节点）
        real = [n for n in sub.nodes
                if n["kind"] == "address" and n["first_layer"] >= 0]
        ghost = [n for n in sub.nodes
                 if n["kind"] == "address" and n["first_layer"] == -1]
        assert len(real) <= CLOSURE_NODE_CAP == 200
        assert len(real) + len(ghost) == 251  # 引用完整性：每条边端点都有节点


# ---------------------------------------------------------------------------
# Retriever 精排组合 — mini leave-one-out 业务场景
# ---------------------------------------------------------------------------
from backend.retrieval.retriever import (
    PatternCandidate,
    Retriever,
    subgraphresult_to_canonical,
)


class FakeResult:
    def __init__(self, rows): self._rows = rows
    def scalars(self): return self
    def all(self): return self._rows
    def mappings(self): return self


class FakeSession:
    def __init__(self, recall_rows):
        self.recall_rows = recall_rows

    def execute(self, stmt, params=None):
        if "DISTINCT embedding_model" in str(stmt):
            return FakeResult([])
        return FakeResult(self.recall_rows)


class FakeSettings:
    """与 config 默认值一致的基准组合 (0.1 cos, 0.1 wljac, 0.8 ov)。"""
    embedding_provider = "stub"
    embedding_model = "test-model"
    embedding_dim = 64
    w_struct = 0.7
    w_semantic = 0.3
    retrieval_recall_limit = 10
    retrieval_top_k = 4
    wl_iterations = 4
    channel_w_cos = 0.1
    channel_w_align = 0.0
    channel_w_wljac = 0.1
    channel_w_fp = 0.0
    channel_w_ov = 0.8


def _sibling_corpus():
    """两个同源兄弟闭包（共享同一区域地址）+ 一个无关干扰项。"""
    sib_a = _chain_canon(8, seed="shared_seed")
    sib_b = _chain_canon(6, seed="shared_seed")   # 同区域、更短
    other = _chain_canon(8, seed="disjoint")      # 地址完全不相交
    return sib_a, sib_b, other


class TestChannelComboRerank:
    def _rows(self):
        sib_a, sib_b, other = _sibling_corpus()
        # 三者 stage1 召回分相同（dist 一致）——排序完全由结构通道决定
        return [
            {"id": "sib_a", "name": "caseA", "description": "",
             "evidence_grade": "A", "source": "lazarus_confirmed",
             "provenance": "confirmed", "canonical_subgraph": sib_a,
             "dist": 0.2, "struct_sim": 0.8, "sem_sim": 0.8},
            {"id": "sib_b", "name": "caseB", "description": "",
             "evidence_grade": "A", "source": "lazarus_confirmed",
             "provenance": "confirmed", "canonical_subgraph": sib_b,
             "dist": 0.2, "struct_sim": 0.8, "sem_sim": 0.8},
            {"id": "other", "name": "noise", "description": "",
             "evidence_grade": "A", "source": "lazarus_confirmed",
             "provenance": "confirmed", "canonical_subgraph": other,
             "dist": 0.2, "struct_sim": 0.8, "sem_sim": 0.8},
        ]

    def test_leave_one_out_sibling_ranks_first(self):
        """查询 = 剪叶克隆（自身行已剔除）→ 同源兄弟进前排、干扰项最后。"""
        sib_a, _, _ = _sibling_corpus()
        query = _prune_leaves(sib_a, frac=0.3)
        rows = self._rows()
        result = Retriever(FakeSession(rows), FakeSettings()).retrieve(query)
        ids = [c.pattern_id for c in result.candidates]
        assert ids[0] in {"sib_a", "sib_b"}
        assert ids[-1] == "other"

    def test_candidate_exposes_channel_scores(self):
        sib_a, _, _ = _sibling_corpus()
        query = _prune_leaves(sib_a, frac=0.3)
        result = Retriever(FakeSession(self._rows()), FakeSettings()).retrieve(query)
        c = result.candidates[0]
        assert isinstance(c, PatternCandidate)
        assert 0.0 <= c.wl_kernel_score <= 1.0
        assert 0.0 <= c.fp_score <= 1.0
        assert 0.0 <= c.ov_score <= 1.0
        # ov 主导：同源候选的 ov 分应接近满分
        assert c.ov_score > 0.9

    def test_align_weight_positive_rejected(self):
        """align 通道未接入：权重 >0 必须显式报错而非静默 0 分。"""
        import copy

        settings = copy.copy(FakeSettings())
        settings.channel_w_align = 0.2
        with pytest.raises(ValueError, match="align"):
            Retriever(FakeSession(self._rows()), settings).retrieve(
                _chain_canon(5))

    def test_weights_not_injectable_via_request(self):
        """通道权重只能来自 Settings——retrieve 签名不得含任何权重参数。"""
        import inspect

        sig = inspect.signature(Retriever.retrieve).parameters
        for forbidden in ("w_cos", "w_ov", "w_wljac", "w_fp", "weights"):
            assert forbidden not in sig

    def test_canonical_conversion_carries_flag_keys(self):
        """SubgraphResult → canonical：三级标签键集与 ingest 同构。"""
        from types import SimpleNamespace as NS

        result = NS(
            nodes=[NS(id="addr:a", kind="address", label="a", first_layer=0,
                      total_received_btc=1.0, total_sent_btc=0.9,
                      utxo_count=1, direct_related_to_lazarus=False,
                      confirmed_downstream=True,
                      probably_lazarus_related=False, is_censored=True)],
            edges=[])
        canon = subgraphresult_to_canonical(result)
        n = canon["nodes"][0]
        assert n["confirmed_downstream"] is True
        assert n["is_censored"] is True
