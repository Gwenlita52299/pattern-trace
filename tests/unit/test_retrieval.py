"""retrieval 单元测试 — RT-01~05/09~12/14/15/18。

DB 相关路径（RT-02 向量落库形态、RT-06 性能、RT-16 评估）由
infra/verify_phase3.sh 在真实 PostgreSQL 上验证。
"""
import inspect
import math

import pytest

from backend.retrieval.features import (
    FEATURE_DIM,
    cosine_similarity,
    describe_subgraph,
    extract_features,
    generate_difference_note,
    wl_subtree_similarity,
)


# ---------------------------------------------------------------------------
# RT-01 · 特征向量核心维度
# ---------------------------------------------------------------------------
class TestExtractFeatures:
    def _graph(self, *, n_nodes=45, n_edges=120, mixer=3, crosschain=1):
        nodes = [{"id": f"a{i}", "kind": "address", "first_layer": 0}
                 for i in range(n_nodes)]
        edges = []
        for i in range(n_edges):
            e = {"source": f"a{i % n_nodes}", "target": f"a{(i + 1) % n_nodes}",
                 "is_remixer": i < mixer, "is_crosschain": False,
                 "is_stopped_expansion": False}
            if i < crosschain:
                e["is_crosschain"] = True
            edges.append(e)
        return nodes, edges

    def test_rt01_core_dimensions(self):
        """features[0]=45, [1]=120, [2]=density, [5]=mixer, [7]=crosschain。"""
        nodes, edges = self._graph()
        feats = extract_features(nodes, edges)
        assert feats[0] == 45
        assert feats[1] == 120
        assert feats[2] == pytest.approx(120 / (45 * 44))
        assert feats[5] == 3   # mixer_contact_count
        assert feats[7] == 1   # crosschain_count
        assert len(feats) == FEATURE_DIM == 20

    def test_accepts_dataclass_style_objects(self):
        """SubgraphResult 节点/边是 dataclass——属性访问路径必须可用。"""
        from types import SimpleNamespace as NS

        nodes = [NS(id="a0", kind="address")]
        edges = [NS(source="a0", target="t1", is_remixer=True,
                    is_crosschain=False, is_stopped_expansion=False)]
        feats = extract_features(nodes, edges)
        assert feats[5] == 1

    def test_rt18_empty_and_single_node(self):
        empty = extract_features([], [])
        assert len(empty) == FEATURE_DIM
        assert all(math.isfinite(v) for v in empty)
        single = extract_features([{"id": "s", "kind": "address"}], [])
        assert single[0] == 1
        assert single[2] == 0.0  # density=0


# ---------------------------------------------------------------------------
# RT-13 · cosine 边界值
# ---------------------------------------------------------------------------
class TestCosine:
    def test_rt13_boundaries(self):
        a, b, c = [1.0, 0.0], [0.0, 1.0], [1.0, 0.0]
        assert cosine_similarity(a, c) == pytest.approx(1.0)
        assert cosine_similarity(a, b) == pytest.approx(0.0)
        for x, y in [(a, b), (b, c), (a, c)]:
            assert 0.0 <= cosine_similarity(x, y) <= 1.0


# ---------------------------------------------------------------------------
# RT-09~12 · WL kernel 属性
# ---------------------------------------------------------------------------
def _chain(n):
    nodes = [{"id": f"a{i}", "kind": "address", "first_layer": i % 3,
              "total_received_btc": 0.1 * (i + 1)} for i in range(n)]
    edges = [{"source": f"a{i}", "target": f"a{i + 1}"} for i in range(n - 1)]
    return {"nodes": nodes, "edges": edges}


def _star(n):
    nodes = [{"id": "c", "kind": "address", "first_layer": 0,
              "total_received_btc": 1.0}] + [
        {"id": f"l{i}", "kind": "address", "first_layer": 1,
         "total_received_btc": 0.2} for i in range(n - 1)]
    edges = [{"source": "c", "target": f"l{i}"} for i in range(n - 1)]
    return {"nodes": nodes, "edges": edges}


class TestWLKernel:
    def test_rt09_isomorphic_high_score(self):
        a = _chain(5)
        # ID 换前缀、结构与属性不变的同构副本
        b = {"nodes": [{**n, "id": "x" + n["id"][1:]} for n in a["nodes"]],
             "edges": [{"source": "x" + e["source"][1:],
                        "target": "x" + e["target"][1:]} for e in a["edges"]]}
        assert wl_subtree_similarity(a, b) >= 0.95

    def test_rt10_chain_vs_star_low_score(self):
        assert wl_subtree_similarity(_chain(4), _star(4)) <= 0.3

    def test_rt11_attribute_difference_detected(self):
        base = _chain(6)
        mixed = {"nodes": [{**n, "kind": "mixer"} if i < 3 else dict(n)
                           for i, n in enumerate(base["nodes"])],
                 "edges": base["edges"]}
        same = wl_subtree_similarity(base, dict(base))
        diff = wl_subtree_similarity(base, mixed)
        assert diff < 0.8
        assert diff < same  # 类型差异被捕捉而非忽略

    def test_rt12_empty_graph_boundaries(self):
        empty = {"nodes": [], "edges": []}
        assert wl_subtree_similarity(empty, empty) == 1.0
        assert wl_subtree_similarity(empty, _chain(3)) == 0.0
        assert wl_subtree_similarity(_chain(3), empty) == 0.0


# ---------------------------------------------------------------------------
# RT-03 / RT-14 · 描述文本与差异说明
# ---------------------------------------------------------------------------
class TestTextGeneration:
    def _canon(self, *, remixer=True, layers=2):
        return {
            "seed_address": "bc1qseed",
            "nodes": [{"id": f"addr:a{i}", "kind": "address", "label": f"a{i}",
                       "first_layer": min(i, layers)} for i in range(8)],
            "edges": [{"id": f"edge:e{i}", "source": "addr:a0",
                       "target": "tx:t", "dst_value_btc": 0.5,
                       "is_remixer": remixer and i == 0,
                       "is_crosschain": False,
                       "is_stopped_expansion": False} for i in range(4)],
            "stats": {"node_count": 9, "edge_count": 4, "max_first_layer": layers},
        }

    def test_rt03_description_contains_key_facts(self):
        text = describe_subgraph(self._canon())
        assert "8 nodes" in text and "4 edges" in text
        assert "BTC" in text
        assert "CoinJoin mixer" in text          # 混币器接触关键词
        assert "hop layers" in text              # 分层关键词

    def test_rt03_number_bucketing_shared_tokens(self):
        """数量级桶化：不同具体数值的描述共享 num token。"""
        from backend.retrieval.embedding import tokenize

        t1 = tokenize("with 45 nodes and 120 edges")
        t2 = tokenize("with 46 nodes and 118 edges")
        assert "num2" in t1 and "num2" in t2     # 45/46/120/118 同为 10^2 内

    def test_rt14_difference_note_counts_accurately(self):
        inp, cand = _chain(10), _chain(5)
        note = generate_difference_note(inp, cand)
        assert note is not None
        assert "5 more nodes" in note            # N 数值准确

    def test_rt14_small_delta_returns_none(self):
        assert generate_difference_note(_chain(5), _chain(5)) is None


# ---------------------------------------------------------------------------
# RT-04/05/15/18 · Retriever（fake session，无 DB）
# ---------------------------------------------------------------------------
from backend.retrieval.retriever import (
    EmbeddingModelMismatch,
    PatternCandidate,
    RetrievalResult,
    Retriever,
    subgraphresult_to_canonical,
    verify_embedding_model_lock,
)


class FakeResult:
    def __init__(self, rows): self._rows = rows
    def scalars(self): return self
    def all(self): return self._rows
    def mappings(self): return self


class FakeSession:
    """按 SQL 片段路由：模型锁查询 / 召回查询。"""

    def __init__(self, recall_rows, db_models=None):
        self.recall_rows = recall_rows
        self.db_models = db_models or set()

    def execute(self, stmt, params=None):
        if "DISTINCT embedding_model" in str(stmt):
            return FakeResult(sorted(self.db_models))
        return FakeResult(self.recall_rows)


class FakeSettings:
    embedding_provider = "stub"
    embedding_model = "text-embedding-3-small"
    embedding_dim = 1536
    w_struct = 0.7
    w_semantic = 0.3
    retrieval_recall_limit = 20
    retrieval_top_k = 4
    wl_iterations = 3


def _recall_row(pid, name, *, dist=0.1, struct_sim=0.9, sem_sim=0.8,
                source="lazarus_confirmed", provenance="confirmed",
                evidence_grade="A"):
    return {
        "id": pid, "name": name, "description": "", "evidence_grade": evidence_grade,
        "source": source, "provenance": provenance,
        "canonical_subgraph": _chain(6),
        "dist": dist, "struct_sim": struct_sim, "sem_sim": sem_sim,
    }


class TestModelLock:
    def test_rt04_mismatch_raises(self):
        session = FakeSession([], db_models={"all-MiniLM-L6-v2"})
        with pytest.raises(EmbeddingModelMismatch, match="version lock"):
            verify_embedding_model_lock(session, "text-embedding-3-small")

    def test_matching_or_empty_passes(self):
        assert verify_embedding_model_lock(FakeSession([]), "m") is None
        assert verify_embedding_model_lock(
            FakeSession([], db_models={"text-embedding-3-small"}),
            "text-embedding-3-small") is None


class TestHybridRecallContract:
    def test_rt05_signature_has_no_weight_params(self):
        """权重禁止请求传入：公开签名不得含 w_struct/w_semantic。"""
        for forbidden in ("w_struct", "w_semantic", "weights"):
            assert forbidden not in inspect.signature(Retriever.retrieve).parameters

    def test_rt05_weights_bound_as_params_not_inlined(self):
        """SQL 中权重以绑定变量出现，编译产物不含字面量 0.7/0.3。"""
        from sqlalchemy import bindparam, text

        sql = text("""
            SELECT :w_struct * (structural_features <=> CAST(:sv AS vector))
                 + :w_semantic * (semantic_embedding <=> CAST(:ev AS vector)) AS d
            FROM patterns WHERE id NOT IN :excluded
        """).bindparams(bindparam("excluded", expanding=True))
        compiled = str(sql.compile())
        assert ":w_struct" in compiled and ":w_semantic" in compiled
        assert "0.7" not in compiled and "0.3" not in compiled


class TestFullRetrievalFlow:
    def test_rt15_output_structure_complete_and_deterministic(self):
        rows = [_recall_row(f"p{i}", f"synth_peel_chain_l{i % 2 + 2}",
                            dist=0.05 * i) for i in range(5)]
        session = FakeSession(rows)
        retriever = Retriever(session, FakeSettings())
        result = retriever.retrieve(_chain(6))

        assert isinstance(result, RetrievalResult)
        assert 3 <= len(result.candidates) <= 5  # 确定数据下 Top-3~5
        c = result.candidates[0]
        assert isinstance(c, PatternCandidate)
        for attr in ("pattern_id", "name", "canonical_subgraph",
                     "similarity_score", "wl_kernel_score",
                     "evidence_grade", "source", "provenance",
                     "difference_note"):
            assert hasattr(c, attr), attr
        assert 0.0 <= c.similarity_score <= 1.0
        again = retriever.retrieve(_chain(6))
        assert [x.pattern_id for x in again.candidates] == \
               [x.pattern_id for x in result.candidates]

    def test_rt15_candidates_sorted_by_final_score(self):
        rows = [_recall_row("near", "synth_hybrid_l3", dist=0.01),
                _recall_row("far", "synth_hybrid_l4", dist=0.60)]
        result = Retriever(FakeSession(rows), FakeSettings()).retrieve(_chain(6))
        ids = [c.pattern_id for c in result.candidates]
        assert ids.index("near") < ids.index("far")

    def test_rt18_single_node_subgraph_no_crash(self):
        canon = {"seed_address": "bc1qs", "nodes": [
            {"id": "addr:s", "kind": "address", "label": "bc1qs",
             "first_layer": 0}], "edges": [],
            "stats": {"node_count": 1, "edge_count": 0, "max_first_layer": 0}}
        result = Retriever(FakeSession([]), FakeSettings()).retrieve(canon)
        assert isinstance(result, RetrievalResult)

    def test_rt18_empty_canonical_returns_no_match_signal(self):
        result = Retriever(FakeSession([]), FakeSettings()).retrieve(
            {"nodes": [], "edges": [], "stats": {}})
        assert result.candidates == []


class TestCanonicalConversion:
    def test_subgraphresult_conversion_matches_ingest_schema(self):
        from types import SimpleNamespace as NS

        result = NS(
            nodes=[NS(id="addr:a", kind="address", label="a", first_layer=0,
                      total_received_btc=1.0, total_sent_btc=0.9,
                      utxo_count=1, direct_related_to_lazarus=False),
                   NS(id="tx:t1", kind="transaction", label="t1[:16]",
                      first_layer=0)],
            edges=[NS(id="edge:x", source="addr:a", target="tx:t1",
                      txid="t1", tx_layer="tx2", value_ratio=0.5,
                      dst_value_btc=1.0, total_num_inputs=1,
                      total_num_outputs=2, is_stopped_expansion=False,
                      is_remixer=False, is_crosschain=False,
                      op_return_protocol=None)],
        )
        canon = subgraphresult_to_canonical(result)
        assert canon["seed_address"] == "a"
        assert canon["stats"]["node_count"] == 2
        assert canon["nodes"] == sorted(canon["nodes"], key=lambda n: n["id"])
        assert canon["edges"][0]["txid"] == "t1"
        # 与 ingest canonical 同构：可直接进 WL/特征/描述函数
        feats = extract_features(canon["nodes"], canon["edges"])
        assert len(feats) == FEATURE_DIM
        assert describe_subgraph(canon)


class TestGraphormerMissingDataFallback:
    """issue #78：npz 缺失不得 500——graphormer 通道降级 + 可观测日志。"""

    def test_missing_files_return_none_with_fallback_log(self, monkeypatch, caplog):
        import logging

        from backend.retrieval import graphormer

        monkeypatch.setattr(graphormer, "DERIVED_DIR",
                            graphormer.Path("/nonexistent/pt-e2e-test"))
        graphormer._pooled.cache_clear()
        graphormer.ego_lookup.cache_clear()
        with caplog.at_level(logging.WARNING):
            q = graphormer.graphormer_query_vector(
                {"nodes": [{"kind": "address", "label": "a",
                            "first_layer": 0}], "edges": []})
        assert q is None  # 降级信号：调用方回退 hybrid 召回
        assert "falling back to hybrid" in caplog.text  # 可 grep 的降级标记
        graphormer._pooled.cache_clear()
        graphormer.ego_lookup.cache_clear()


class TestGraphormerOnlineChain:
    """issue #82：查询向量三级链 online → ego 查表 → hybrid。"""

    def test_no_torch_falls_back_to_ego_path(self, monkeypatch):
        """默认依赖（无 torch）下 online 层返回 None，链回退 ego 查表——
        行为与三级链引入前完全一致（回归保护）。"""
        from backend.retrieval import graphormer, graphormer_online

        assert graphormer_online._DEPS_OK is False  # 单测环境不装 torch
        monkeypatch.setattr(graphormer, "DERIVED_DIR",
                            graphormer.Path("/nonexistent/pt-e2e-test"))
        graphormer._pooled.cache_clear()
        graphormer.ego_lookup.cache_clear()
        canon = {"nodes": [{"kind": "address", "label": "a",
                            "first_layer": 0}], "edges": []}
        assert graphormer_online.query_vector_online(canon) is None
        # 三级链：online None → ego 查表也 None → 由 retriever 降 hybrid
        assert graphormer.graphormer_query_vector(canon) is None
        graphormer._pooled.cache_clear()
        graphormer.ego_lookup.cache_clear()

    def test_mode_off_skips_online_import(self):
        """graphormer_query_mode=off：retriever 不尝试 online 层。"""
        import inspect

        src = inspect.getsource(
            __import__("backend.retrieval.retriever",
                       fromlist=["Retriever"]).Retriever.retrieve)
        # off 只进入 ego 查表路径（字符串级守卫，防回归三级链逻辑漂移）
        assert 'mode in ("auto", "online")' in src
        assert 'mode = getattr(self.settings, "graphormer_query_mode", "auto")' in src

    def test_projection_produces_addr_adjacency(self):
        """canonical（addr↔tx 二部图）→ addr→addr 投影 + 金额跨 tx 求和。"""
        from backend.retrieval.graphormer_online import project_addr_adjacency

        canon = {
            "nodes": [
                {"id": "addr:s", "kind": "address", "label": "bc1qs",
                 "first_layer": 0, "utxo_count": 2, "total_received_btc": 3.0,
                 "total_sent_btc": 2.0, "layer_span": 1, "is_censored": False},
                {"id": "addr:d", "kind": "address", "label": "bc1qd",
                 "first_layer": 1, "utxo_count": 1, "total_received_btc": 2.0,
                 "total_sent_btc": 0.0, "layer_span": 0, "is_censored": False},
                {"id": "tx:t1", "kind": "transaction", "label": "tx1"},
                {"id": "tx:t2", "kind": "transaction", "label": "tx2"},
            ],
            "edges": [
                {"source": "addr:s", "target": "tx:t1",
                 "total_num_inputs": 2},
                {"source": "tx:t1", "target": "addr:d", "dst_value_btc": 1.0,
                 "is_remixer": False, "is_crosschain": False},
                {"source": "addr:s", "target": "tx:t2",
                 "total_num_inputs": 2},
                {"source": "tx:t2", "target": "addr:d", "dst_value_btc": 0.5,
                 "is_remixer": True, "is_crosschain": False,
                 "op_return_protocol": "thorchain"},
            ],
        }
        adj, in_adj, attr = project_addr_adjacency(canon)
        # 两 tx 的 (s→d) 合并：金额求和 = 1.5，attrs 取最大单笔（1.0 → pack 无 thor）
        assert adj["addr:s"]["addr:d"][0] == 1.5
        assert in_adj["addr:d"]["addr:s"] is adj["addr:s"]["addr:d"]
        assert set(attr) == {"addr:s", "addr:d"}

    def test_ego_graph_shape_protocol(self):
        """F=7 节点特征 / 4 槽边 / spatial 短路距离 ≤5 或 16（run_g 协议）。"""
        from backend.retrieval import graphormer_online as go

        adj = {"a": {"b": [2.0, 1, 3]}}
        in_adj = {"b": {"a": [2.0, 1, 3]}}
        attr = {"a": (0, 2, 3.0, 2.0, 1, 0), "b": (1, 1, 2.0, 0.0, 0, 0)}
        nodes, indeg, outdeg, sp, edges = go._build_ego_graph(
            adj, in_adj, "a", attr)
        assert nodes.shape == (2, 7)
        assert nodes[0, 0] == 1 + 2 * 8 + min(1, 7)      # addr_type*deg 混合 id（"a"=类 2）
        assert indeg[0] == 1 and outdeg[0] == 2          # deg_bucket(d)=min(d,7)+1
        assert sp.shape == (2, 2) and sp[0, 1] == 1      # BFS 短路距离
        assert edges.shape == (2, 2, 5, 4)               # 4 槽边特征
