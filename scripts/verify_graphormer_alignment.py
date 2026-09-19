"""Graphormer 在线前向 vs 库内向量 对齐验证。

口径 A（全库邻接）：完全复刻离线管线（run_g.py use_full + seed_subgraph_agg.py），
验证特征编码复刻正确——与库内 graphormer_embedding 的 cos 应 ≈1.0。
口径 B（仅闭包内邻接）：live 协议现实上界——在线只有分析子图内邻接。
"""
import os
import sys
import time
from collections import defaultdict, deque

import numpy as np
import pyarrow.parquet as pq

REPO = sys.argv[1] if len(sys.argv) > 1 else "."
DATA = f"{REPO}/ingest/seed/graphormer_v2"
N_SEEDS = 5
BATCH = 256
MAX_IN_NB = 15
MAX_OUT_NB = 15
SPATIAL_CAP = 5
SPATIAL_UNREACH = 16
MAX_DEPTH = 4
NODE_CAP = 200

os.environ["HF_HOME"] = "/tmp/gorm_cache"
os.environ["HF_ENDPOINT"] = "https://huggingface.co"

PROTO_IDS = {}


def proto_id(p):
    if not p:
        return 0
    if p not in PROTO_IDS:
        PROTO_IDS[p] = len(PROTO_IDS) + 1
    return PROTO_IDS[p]


def addr_type(a):
    if a.startswith("bc1"):
        return 0
    if a.startswith("3"):
        return 1
    return 2


def deg_bucket(d):
    return min(d, 7) + 1


def log_bucket(v):
    if v is None or v <= 0:
        return 1
    return int(min(max(np.floor(np.log10(v)) + 3, 1), 6))


def utxo_bucket(u):
    if u <= 1:
        return 1
    if u <= 3:
        return 2
    if u <= 10:
        return 3
    if u <= 50:
        return 4
    return 5


t0 = time.time()
# ---- nodes 全表特征 ----
addrs = []
feats = []
cols_need = ["address", "first_layer", "utxo_count", "total_received_btc",
             "total_sent_btc", "is_censored", "layer_span_max", "out_degree_addr"]
for b in pq.ParquetFile(f"{DATA}/subgraph_nodes.parquet").iter_batches(
        columns=cols_need, batch_size=500_000):
    names = b.schema.names
    for r in zip(*[b.column(i).to_pylist() for i in range(b.num_columns)]):
        d = dict(zip(names, r))
        addrs.append(d["address"])
        feats.append((d["first_layer"], d["utxo_count"], d["total_received_btc"],
                      d["total_sent_btc"], d["layer_span_max"], d["is_censored"]))
addr2idx = {a: i for i, a in enumerate(addrs)}
print(f"nodes {len(addrs):,} ({time.time()-t0:.0f}s)", flush=True)

# ---- 全库邻接（run_g 语义：金额求和 + 最大单笔贡献 attrs）+ 闭包邻接 ----
adj_rg = defaultdict(dict)     # run_g: adj[src][dst] = [sum_v, pack, n_in, td0]
adj_cl = defaultdict(list)     # agg 闭包: list[(dst_idx, v, thor, xc)] 保留 v=None
t0 = time.time()
ef = pq.ParquetFile(f"{DATA}/subgraph_edges.parquet")
need_cols = ["src_address", "dst_address", "dst_value_btc", "is_crosschain",
             "is_remixer", "op_return_protocol", "time_delta",
             "total_num_inputs", "is_stopped_expansion"]
for b in ef.iter_batches(columns=need_cols, batch_size=1_000_000):
    cols = {n: b.column(n).to_pylist() for n in b.schema.names}
    for s, t, v, xc, rm, pr, td, ni, st in zip(
            cols["src_address"], cols["dst_address"], cols["dst_value_btc"],
            cols["is_crosschain"], cols["is_remixer"], cols["op_return_protocol"],
            cols["time_delta"], cols["total_num_inputs"],
            cols["is_stopped_expansion"]):
        si = addr2idx.get(s)
        di = addr2idx.get(t) if t is not None else None
        pack = proto_id(pr) * 4 + (2 if xc else 0) + (1 if rm else 0)
        td0 = td[0] if td and len(td) > 0 else 0
        if si is None:
            continue
        # 闭包邻接：dst 不在库内(-1)也保留（ext 边，ext 占比用）
        v_cl = v if v is not None else 0.0
        adj_cl[si].append((di if di is not None else -1, v_cl,
                           1 if (pr and "thorchain" in pr) else 0,
                           1 if xc else 0))
        if di is None or v is None:
            continue
        d_ = adj_rg[si].get(di)
        if d_ is None:
            adj_rg[si][di] = [v, pack, ni, td0]
        else:
            d_[0] += v
            if v >= d_[0] - v:
                d_[1] = pack
                d_[2] = ni
                d_[3] = td0
print(f"adjacency: run_g {len(adj_rg):,} / closure {len(adj_cl):,} "
      f"({time.time()-t0:.0f}s)", flush=True)

in_adj = defaultdict(dict)
for s, m in adj_rg.items():
    for di, attrs in m.items():
        in_adj[di][s] = attrs


def pick(adj_src, in_adj_src, ego):
    outs = sorted(adj_src.get(ego, {}).items(), key=lambda kv: -kv[1][0])[:MAX_OUT_NB]
    ins = sorted(in_adj_src.get(ego, {}).items(), key=lambda kv: -kv[1][0])[:MAX_IN_NB]
    return [a for a, _ in ins], [a for a, _ in outs]


def closure(seed_idx, adj_src):
    """seed_subgraph_agg 协议：出边 BFS 深度 ≤4、cap 200、含库外 ext 边计数。"""
    depth_of = {seed_idx: 0}
    members = [seed_idx]
    frontier = [seed_idx]
    edges_lvl = [[], [], [], [], []]
    lvl = 0
    while frontier and lvl < MAX_DEPTH and len(members) < NODE_CAP:
        nxt = []
        for u in frontier:
            for e in adj_src.get(u, ()):
                edges_lvl[lvl + 1].append(e)
                di = e[0]
                if di == -1 or di in depth_of:
                    continue
                if len(members) >= NODE_CAP:
                    break
                depth_of[di] = lvl + 1
                members.append(di)
                nxt.append(di)
        frontier = nxt
        lvl += 1
    return members, depth_of, edges_lvl


def closure_scal(members, depth_of, edges_lvl):
    """16 标量（seed_subgraph_agg 权威定义）。"""
    from collections import Counter
    all_edges = [e for lst in edges_lvl[1:] for e in lst]
    n_edges = len(all_edges)
    tot_v1 = sum(e[1] for e in edges_lvl[1])
    n1 = len(edges_lvl[1])
    vals1 = Counter(round(e[1], 8) for e in edges_lvl[1])
    eq1 = (max(vals1.values()) / n1) if n1 else 0.0
    n_thor = sum(e[2] for e in all_edges)
    n_xc = sum(e[3] for e in all_edges)
    ext_n = sum(1 for e in all_edges if e[0] == -1)
    depths = np.array([depth_of[m] for m in members], np.float32)
    return np.array([
        np.log10(1 + len(members)), np.log10(1 + n_edges),
        np.log10(1 + len(edges_lvl[1])), np.log10(1 + len(edges_lvl[2])),
        np.log10(1 + len(edges_lvl[3])), np.log10(1 + len(edges_lvl[4])),
        np.log10(1 + tot_v1), eq1,
        n_thor / n_edges if n_edges else 0.0,
        n_xc / n_edges if n_edges else 0.0,
        ext_n / n_edges if n_edges else 0.0,
        depths.mean() if len(depths) else 0.0,
        np.log10(1 + sum(feats[m][2] for m in members)),
        np.log10(1 + sum(feats[m][3] for m in members)),
        np.log10(1 + np.mean([feats[m][1] for m in members])),
        np.mean([feats[m][5] for m in members]),
    ], np.float64)


def build_graph(adj_src, in_adj_src, ego, ins, outs):
    """run_g.py build_graph use_full=True（F=7 / S=4）逐行复刻。"""
    members = [ego] + ins + [a for a in outs if a not in ins]
    idx = {a: i for i, a in enumerate(members)}
    n = len(members)
    dir_edges = defaultdict(dict)
    for s in members:
        si_local = idx[s]
        for d_idx, attrs in adj_src.get(s, {}).items():
            if d_idx in idx:
                dir_edges[si_local][idx[d_idx]] = attrs

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
        fa = feats[a]
        nodes[i, 0] = 1 + addr_type(addrs[a]) * 8 + min(len(adj_src.get(a, {})), 7)
        nodes[i, 1] = 1 + min(int(fa[0]), 3)
        nodes[i, 2] = 1 + utxo_bucket_f(fa[1])
        nodes[i, 3] = 1 + log_bucket(fa[2])
        nodes[i, 4] = 1 + log_bucket(fa[3])
        nodes[i, 5] = 1 + min(int(fa[4]), 3)
        nodes[i, 6] = 1 + int(fa[5])
    indeg = np.zeros(n, np.int16)
    outdeg = np.zeros(n, np.int16)
    for a, i in idx.items():
        indeg[i] = deg_bucket(len(in_adj_src.get(a, {})))
        outdeg[i] = deg_bucket(len(adj_src.get(a, {})))
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
            path = _path(dir_edges, i, j)
            for step, (u, v) in enumerate(path[:SPATIAL_CAP]):
                attrs = dir_edges[u].get(v)
                if attrs is None:
                    continue
                val_b = log_bucket(attrs[0])
                edges[i, j, step, 0] = val_b
                edges[i, j, step, 1] = val_b
                edges[i, j, step, 2] = attrs[1] + 1
                edges[i, j, step, 3] = 1 + int(min(np.floor(np.log10(max(attrs[3], 0) + 1)), 8))
    return (nodes, indeg, outdeg, sp, edges)


def utxo_bucket_f(u):
    if u <= 1:
        return 1
    if u <= 3:
        return 2
    if u <= 10:
        return 3
    if u <= 50:
        return 4
    return 5


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


def collate(graphs, dev):
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
        "attn_edge_type": torch.zeros(B, n_max, n_max, 2, dtype=torch.long, device=dev),
    }


# ---- 模型 ----
try:
    import torch  # noqa: E402
    from transformers import GraphormerModel, GraphormerConfig  # noqa: E402
    from huggingface_hub import hf_hub_download  # noqa: E402
except ImportError as exc:
    raise SystemExit(
        "缺少可选依赖：uv pip install torch 'transformers==4.40.*' "
        "huggingface_hub pyarrow（issue #82 graphormer-online 组）") from exc

MODEL_NAME = "clefourrier/graphormer-base-pcqm4mv2"
config = GraphormerConfig.from_pretrained(MODEL_NAME)
model = GraphormerModel(config)
ckpt = hf_hub_download(MODEL_NAME, "pytorch_model.bin")
sd = torch.load(ckpt, map_location="cpu", weights_only=False)
sd = {k[len("encoder."):]: v for k, v in sd.items() if k.startswith("encoder.")}
missing, unexpected = model.load_state_dict(sd, strict=False)
assert not missing and not unexpected, (len(missing), len(unexpected))
model.eval()
dev = "mps" if torch.backends.mps.is_available() else "cpu"
model.to(dev)
print("device:", dev, flush=True)


npz = np.load(f"{REPO}/ingest/seed/graphormer_v2/derived/graphormer_pooled.npz")
kb_seeds, scal_mu, scal_sd = npz["seeds"], npz["scal_mu"], npz["scal_sd"]
kb_vec = npz["pooled"]
# confirmed_candidates 里挑 confirmed=1 且在库内的 seed
cc = pq.read_table(f"{DATA}/derived/confirmed_candidates.parquet").to_pydict()
conf_seeds = [s for s, c in zip(cc["seed_address"], cc["confirmed_members"])
              if c and s in set(kb_seeds.tolist())]
sel = conf_seeds[:N_SEEDS]
print("seeds:", [s[:14] for s in sel], flush=True)

seed_idx = {a: i for i, a in enumerate(addrs)}
results = {}

for scope in ("FULL_ADJ", "CLOSURE_ONLY"):
    t0 = time.time()
    cos_list = []
    for seed_addr in sel:
        seed = addr2idx[seed_addr]
        members, depth_of, edges_lvl = closure(seed, adj_cl)
        if scope == "CLOSURE_ONLY":
            mset = set(members)
            adj_s = {s: {di: a for di, a in m.items() if di in mset}
                     for s, m in adj_rg.items() if s in mset}
            in_s = {di: {s: a for s, a in m.items() if s in mset}
                    for di, m in in_adj.items() if di in mset}
        else:
            adj_s, in_s = adj_rg, in_adj
        # 成员 ego 前向
        graphs = []
        for m in members:
            ins, outs = pick(adj_s, in_s, m)
            graphs.append((m, build_graph(adj_s, in_s, m, ins, outs)))
        ego_vec = {}
        with torch.no_grad():
            for s_ in range(0, len(graphs), BATCH):
                chunk = graphs[s_:s_ + BATCH]
                batch = collate([g for _, g in chunk], dev)
                res = model.graph_encoder(
                    batch["input_nodes"], batch["input_edges"], batch["attn_bias"],
                    batch["in_degree"], batch["out_degree"], batch["spatial_pos"],
                    batch["attn_edge_type"], last_state_only=True)
                inner = res[0] if isinstance(res, tuple) else res
                final = inner[-1] if isinstance(inner, list) else inner
                h = final.transpose(0, 1).float()
                for bi, (m, _) in enumerate(chunk):
                    ego_vec[m] = h[bi, 1].float().cpu().numpy()
        # 池化（深度衰减）+ scal z + L2
        w = np.array([1.0 / (1.0 + depth_of[m]) for m in members], np.float32)[:, None]
        pool = (w * np.vstack([ego_vec[m] for m in members])).sum(axis=0) / w.sum()
        scal = closure_scal(members, depth_of, edges_lvl)
        q = np.concatenate([pool, (scal - scal_mu) / scal_sd])
        q /= max(float(np.linalg.norm(q)), 1e-9)
        kb = kb_vec[list(kb_seeds).index(seed_addr)]
        cos = float(np.dot(q, kb) / (np.linalg.norm(q) * np.linalg.norm(kb)))
        cos_list.append(cos)
        print(f"  [{scope}] {seed_addr[:14]}… cos={cos:.4f} "
              f"members={len(members)} ({time.time()-t0:.0f}s)", flush=True)
    results[scope] = cos_list
    print(f"{scope}: mean={np.mean(cos_list):.4f} min={min(cos_list):.4f}\n", flush=True)

print("=== SUMMARY ===")
for scope, lst in results.items():
    print(f"{scope}: mean cos={np.mean(lst):.4f}  min={min(lst):.4f}  n={len(lst)}")

# 验收判定（issue #82）：FULL_ADJ 口径 cos ≥0.99 才算编码复刻正确
full = results.get("FULL_ADJ", [])
ok = full and min(full) >= 0.99
print(f"\nGATE: FULL_ADJ min cos={min(full) if full else 'n/a'} "
      f"→ {'PASS' if ok else 'FAIL'}")
sys.exit(0 if ok else 1)
