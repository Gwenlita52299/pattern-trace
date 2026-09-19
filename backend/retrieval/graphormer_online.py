"""在线 Graphormer ego 前向（issue #82）— live 子图的全覆盖查询向量。

协议逐行照抄离线管线（docs/graphormer-usage.md，last_version/graphormer_test/
run_g.py use_full=True + seed_subgraph_agg.py），对齐验证 FULL_ADJ cos=1.0000：

- canonical（addr↔tx 二部图）投影为 addr→addr 邻接：对每个 tx 的输入地址 i
  与输出地址 o 生成边 (i→o)，金额取 o 的 dst_value_btc 跨 tx 求和，attrs
  取最大单笔贡献（pack=proto*4+2*crosschain+1*remixer，与 run_g.build_adj 同型）
- 对子图内每个地址构造 ego 图（≤15 入 + ≤15 出邻居，按金额 top），前向取
  ego 节点 hidden state（768d）
- 查询向量 = Σ wᵢ·ego[aᵢ] / Σwᵢ（wᵢ=1/(1+first_layerᵢ)，与查询侧 ego 池化
  同协议）+ 16 标量 z（scal_features_from_canonical 复用）→ L2 → 784d

诚实边界（graphormer-usage.md §6 同级记录）：
- 零样本外推（PCQM4Mv2 预训练权重，未在比特币图上微调）
- 在线 ego 的邻居上下文只来自当前分析子图（离线是全库 6M 边）——对齐验证
  CLOSURE_ONLY 口径 cos≈0.975，是 live 协议的现实上界
- canonical 边无 time_delta，slot3 以 0 兜底（与离线「无时间差」路径同语义）
- 依赖 torch/transformers==4.40.*（optional group graphormer-online），缺失
  或前向异常一律返回 None → 调用方回退 ego 查表 / hybrid（三级链）
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque

import numpy as np

log = logging.getLogger(__name__)

MODEL_NAME = "clefourrier/graphormer-base-pcqm4mv2"
MAX_IN_NB = 15
MAX_OUT_NB = 15
SPATIAL_CAP = 5
SPATIAL_UNREACH = 16
# canonical 节点数硬上限（builder max_total_nodes=200）远小于模型 max_nodes=512
MAX_NODES = 512

try:  # torch/transformers 属于 optional 依赖——缺失是合法形态（回退链下一级）
    import torch
    from transformers import GraphormerConfig, GraphormerModel

    _DEPS_OK = True
    _IMPORT_ERROR = ""
except Exception as _exc:  # noqa: BLE001
    _DEPS_OK = False
    _IMPORT_ERROR = str(_exc)

_MODEL = None
_PROTO_IDS: dict[str, int] = {}


def proto_id(p: str | None) -> int:
    if not p:
        return 0
    if p not in _PROTO_IDS:
        _PROTO_IDS[p] = len(_PROTO_IDS) + 1
    return _PROTO_IDS[p]


def addr_type(a: str) -> int:
    if a.startswith("bc1"):
        return 0
    if a.startswith("3"):
        return 1
    return 2


def deg_bucket(d: int) -> int:
    return min(d, 7) + 1


def log_bucket(v) -> int:
    if v is None or v <= 0:
        return 1
    return int(min(max(np.floor(np.log10(v)) + 3, 1), 6))


def utxo_bucket(u) -> int:
    if u <= 1:
        return 1
    if u <= 3:
        return 2
    if u <= 10:
        return 3
    if u <= 50:
        return 4
    return 5


# ------------------------------------------------------------------
# canonical → addr 邻接
# ------------------------------------------------------------------
def project_addr_adjacency(canon: dict) -> tuple[dict, dict, dict]:
    """canonical（addr↔tx 二部图）→ addr→addr 邻接（run_g.build_adj 语义）。

    返回 (adj, in_adj, node_attr)：
    - adj[src_addr][dst_addr] = [sum_value, pack, n_inputs]（跨 tx 金额求和，
      attrs 取最大单笔贡献——run_g.py:89-97 同型）
    - node_attr[addr] = (first_layer, utxo_count, recv, sent, layer_span,
      is_censored)（F=7 节点特征源）
    """
    edges = canon.get("edges") or []
    nodes = canon.get("nodes") or []
    addr_nodes = [n for n in nodes if (n.get("kind") or "") == "address"]
    tx_nodes = {n.get("id") for n in nodes if (n.get("kind") or "") == "transaction"}
    node_attr = {
        n.get("id"): (max(int(n.get("first_layer") or 0), 0),
                      max(float(n.get("utxo_count") or 0), 0.0),
                      float(n.get("total_received_btc") or 0.0),
                      float(n.get("total_sent_btc") or 0.0),
                      max(int(n.get("layer_span") or 0), 0),
                      1 if n.get("is_censored") else 0)
        for n in addr_nodes
    }
    # tx 节点两侧的 addr 引用：in（addr→tx，带 src_value）与 out（tx→addr）
    tx_in: dict[str, list] = defaultdict(list)
    tx_out: dict[str, list] = defaultdict(list)
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s in tx_nodes and t not in tx_nodes:
            tx_out[s].append(e)
        elif t in tx_nodes and s not in tx_nodes:
            tx_in[t].append(e)
    adj: dict[str, dict[str, list]] = defaultdict(dict)
    for t, outs in tx_out.items():
        in_addrs = [e.get("source") for e in tx_in.get(t, [])]
        n_inputs = max((int(e.get("total_num_inputs") or 0)
                        for e in tx_in.get(t, [])), default=0)
        for o in outs:
            dst = o.get("target")
            if dst not in node_attr:
                continue
            v = float(o.get("dst_value_btc") or 0.0)
            pack = proto_id(o.get("op_return_protocol")) * 4 \
                + (2 if o.get("is_crosschain") else 0) \
                + (1 if o.get("is_remixer") else 0)
            for i in in_addrs:
                if i == dst or i not in node_attr:
                    continue
                d_ = adj[i].get(dst)
                if d_ is None:
                    adj[i][dst] = [v, pack, n_inputs]
                else:
                    d_[0] += v
                    if v >= d_[0] - v:  # attrs 取最大单笔贡献
                        d_[1] = pack
                        d_[2] = n_inputs
    in_adj: dict[str, dict[str, list]] = defaultdict(dict)
    for s, m in adj.items():
        for d, attrs in m.items():
            in_adj[d][s] = attrs
    return adj, in_adj, node_attr


# ------------------------------------------------------------------
# ego 图构造（run_g.build_graph use_full=True 逐行复刻）
# ------------------------------------------------------------------
def _pick(adj, in_adj, ego) -> tuple[list, list]:
    outs = sorted(adj.get(ego, {}).items(), key=lambda kv: -kv[1][0])[:MAX_OUT_NB]
    ins = sorted(in_adj.get(ego, {}).items(), key=lambda kv: -kv[1][0])[:MAX_IN_NB]
    return [a for a, _ in ins], [a for a, _ in outs]


def _path(dir_edges, i, j):
    parent = {i: None}
    q = deque([i])
    while q and j not in parent:
        u = q.popleft()
        for v in dir_edges.get(u, {}):
            if v not in parent:
                parent[v] = u
                q.append(v)
    if j not in parent:
        return []
    path, cur = [], j
    while parent[cur] is not None:
        path.append((parent[cur], cur))
        cur = parent[cur]
    path.reverse()
    return path


def _build_ego_graph(adj, in_adj, ego, node_attr) -> tuple:
    ins, outs = _pick(adj, in_adj, ego)
    members = [ego] + ins + [a for a in outs if a not in ins]
    idx = {a: i for i, a in enumerate(members)}
    n = len(members)
    dir_edges: dict[int, dict[int, list]] = defaultdict(dict)
    for s in members:
        for d, attrs in adj.get(s, {}).items():
            if d in idx:
                dir_edges[idx[s]][idx[d]] = attrs

    dist = [[SPATIAL_UNREACH] * n for _ in range(n)]
    for src_i in range(n):
        dist[src_i][src_i] = 0
        q = deque([src_i])
        while q:
            u = q.popleft()
            if dist[u][src_i] >= SPATIAL_CAP:
                continue
            for v in dir_edges.get(u, {}):
                if dist[v][src_i] == SPATIAL_UNREACH:
                    dist[v][src_i] = dist[u][src_i] + 1
                    q.append(v)

    F, S = 7, 4
    nodes = np.zeros((n, F), np.int16)
    for a, i in idx.items():
        fa = node_attr[a]
        nodes[i, 0] = 1 + addr_type(a) * 8 + min(len(adj.get(a, {})), 7)
        nodes[i, 1] = 1 + min(int(fa[0]), 3)
        nodes[i, 2] = 1 + utxo_bucket(fa[1])
        nodes[i, 3] = 1 + log_bucket(fa[2])
        nodes[i, 4] = 1 + log_bucket(fa[3])
        nodes[i, 5] = 1 + min(int(fa[4]), 3)
        nodes[i, 6] = 1 + int(fa[5])
    indeg = np.zeros(n, np.int16)
    outdeg = np.zeros(n, np.int16)
    for a, i in idx.items():
        indeg[i] = deg_bucket(len(in_adj.get(a, {})))
        outdeg[i] = deg_bucket(len(adj.get(a, {})))
    sp = np.zeros((n, n), np.int8)
    for i in range(n):
        for j in range(n):
            if i != j:
                sp[i, j] = dist[j][i]
    edges = np.zeros((n, n, SPATIAL_CAP, S), np.int8)
    for i in range(n):
        for j in range(n):
            if i == j or dist[j][i] in (0, SPATIAL_UNREACH) or dist[j][i] > SPATIAL_CAP:
                continue
            for step, (u, v) in enumerate(_path(dir_edges, i, j)[:SPATIAL_CAP]):
                attrs = dir_edges[u].get(v)
                if attrs is None:
                    continue
                val_b = log_bucket(attrs[0])
                edges[i, j, step, 0] = val_b
                edges[i, j, step, 1] = val_b
                edges[i, j, step, 2] = attrs[1] + 1
                # canonical 无 time_delta：以 0 兜底（离线「无时间差」同语义）
                edges[i, j, step, 3] = 1 + int(min(np.floor(np.log10(0 + 1)), 8))
    return (nodes, indeg, outdeg, sp, edges)


# ------------------------------------------------------------------
# 模型与前向
# ------------------------------------------------------------------
def _load_model():
    global _MODEL
    if _MODEL is None:
        config = GraphormerConfig.from_pretrained(MODEL_NAME)
        model = GraphormerModel(config)
        import huggingface_hub

        ckpt = huggingface_hub.hf_hub_download(MODEL_NAME, "pytorch_model.bin")
        sd = torch.load(ckpt, map_location="cpu", weights_only=False)
        sd = {k[len("encoder."):]: v for k, v in sd.items()
              if k.startswith("encoder.")}
        missing, unexpected = model.load_state_dict(sd, strict=False)
        assert not missing and not unexpected, (len(missing), len(unexpected))
        model.eval()
        dev = "mps" if getattr(torch.backends, "mps", None) is not None \
            and torch.backends.mps.is_available() else "cpu"
        model.to(dev)
        _MODEL = (model, dev)
    return _MODEL


def query_vector_online(canon: dict) -> np.ndarray | None:
    """canonical 子图 → 在线 ego 前向查询向量（784d）。

    任何异常（依赖缺失 / 超限 / OOM / MPS 不可用）→ 返回 None，
    调用方回退 ego 查表 / hybrid（retriever 三级链）。
    """
    if not _DEPS_OK:
        return None
    try:
        return _query_vector_online(canon)
    except Exception:  # noqa: BLE001 — 在线前向的失败只能回退，不能中断分析
        log.exception("[graphormer_online] forward failed; falling back")
        return None


def _query_vector_online(canon: dict) -> np.ndarray:
    from .graphormer import MIN_EGO_COVERAGE_RATIO, _pooled, scal_features_from_canonical

    pooled = _pooled()
    if pooled is None:
        return None
    _, _, scal_mu, scal_sd = pooled

    adj, in_adj, node_attr = project_addr_adjacency(canon)
    if not node_attr:
        return None
    if len(node_attr) > 512:  # max_nodes 硬上限（canonical 200 上限下不会触发）
        return None

    model, dev = _load_model()
    graphs = [(m, _build_ego_graph(adj, in_adj, m, node_attr))
              for m in node_attr]

    ego_vec: dict[str, np.ndarray] = {}
    with torch.no_grad():
        for s in range(0, len(graphs), 256):
            chunk = graphs[s:s + 256]
            batch = _collate([g for _, g in chunk], dev)
            res = model.graph_encoder(
                batch["input_nodes"], batch["input_edges"], batch["attn_bias"],
                batch["in_degree"], batch["out_degree"], batch["spatial_pos"],
                batch["attn_edge_type"], last_state_only=True)
            inner = res[0] if isinstance(res, tuple) else res
            final = inner[-1] if isinstance(inner, list) else inner
            h = final.transpose(0, 1).float()
            for bi, (m, _) in enumerate(chunk):
                ego_vec[m] = h[bi, 1].float().cpu().numpy()

    addr_nodes = [n for n in (canon.get("nodes") or [])
                  if (n.get("kind") or "") == "address"]
    covered = [n for n in addr_nodes if n.get("id") in ego_vec]
    if len(covered) / len(addr_nodes) < MIN_EGO_COVERAGE_RATIO:
        return None
    w = np.array([1.0 / (1.0 + max(int(n.get("first_layer") or 0), 0))
                  for n in covered], np.float32)[:, None]
    q_pool = (w * np.vstack([ego_vec[n["id"]] for n in covered])).sum(axis=0) / w.sum()
    q_scal = scal_features_from_canonical(canon)
    q = np.concatenate([q_pool, (q_scal - scal_mu) / scal_sd])
    q /= max(float(np.linalg.norm(q)), 1e-9)
    return q.astype(np.float32)


def _collate(graphs, dev):
    B = len(graphs)
    n_max = max(g[0].shape[0] for g in graphs)
    nodes = np.zeros((B, n_max, 7), np.int16)
    indeg = np.zeros((B, n_max), np.int16)
    outdeg = np.zeros((B, n_max), np.int16)
    sp = np.zeros((B, n_max, n_max), np.int8)
    ed = np.zeros((B, n_max, n_max, SPATIAL_CAP, 4), np.int8)
    for bi, g in enumerate(graphs):
        m = g[0].shape[0]
        nodes[bi, :m] = g[0]
        indeg[bi, :m] = g[1]
        outdeg[bi, :m] = g[2]
        sp[bi, :m, :m] = g[3]
        ed[bi, :m, :m] = g[4]

    def t(x):
        return torch.tensor(x.astype(np.int64), dtype=torch.long, device=dev)
    return {
        "input_nodes": t(nodes), "input_edges": t(ed),
        "attn_bias": torch.zeros(B, n_max + 1, n_max + 1, device=dev),
        "in_degree": t(indeg), "out_degree": t(outdeg),
        "spatial_pos": t(sp),
        "attn_edge_type": torch.zeros(B, n_max, n_max, 2,
                                      dtype=torch.long, device=dev),
    }
