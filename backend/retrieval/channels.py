"""benchmark 对齐的精排通道 — 与 last_version/graphormer_test
benchmark_unseen_sim.py（业务级 unseen-similarity leave-one-out 基准）的
通道实现一一对应。

基准最终组合（dev 网格 + 5 轮验证，conf-hit@10 99.0%）：
    0.1·cos + 0.1·wljac + 0.8·ov        (fp/align 权重 0)

通道定义（全部无硬包含、无标签读取——confirmed/probably 是真值，防泄漏）：
  wljac: 方向感知 WL 子树标签 Jaccard，4 轮，轮权 (1,2,3,4)/10，
         scale-invariant 标签：地址 = address|L{层}|U{utxo档}|D{收支主导}，
         交易 = tx|I{输入数}|O{输出数}（均 log 桶化，±15% 金额扰动不变）
  fp   : 金额/收款/UTXO 多重集 Jaccard + 计数向量 L1 亲近度的均值
  ov   : 地址成员重叠 |q∩c| / min(|q|,|c|)——同一轮 CoinJoin 的参与者
         地址必然重合，是「同源案例」的业务级证据（基准报告 §5）

实现为单一指纹代码路径（build_fingerprint → scores_from_fingerprints），
canonical 版本只是入口包装——避免两份实现漂移。指纹可 JSON 序列化、
ingest 时预计算入库（retrieval_fingerprint 列），rerank 无需回传
大体积 canonical JSONB。

cos 通道 = stage1 召回分（Graphormer pooled cosine），retriever 直接取用。
align 通道需要节点级 embedding 语料（基准用 graphormer ego embedding，
查询侧已由 retrieval.graphormer 提供成员向量；候选侧 per-node 对齐分
未预计算）——权重 >0 时由 retriever 显式报错。
"""
from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict

WL_ROUND_WEIGHTS = (0.1, 0.2, 0.3, 0.4)
WL_ITERS = 4


def _num(v) -> float:
    if v is None:
        return 0.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _int(v) -> int:
    return int(_num(v))


def _amt_bucket(a: float) -> int:
    """金额 log10 档（recv 总量档）：0 / 个位 sat 级 / …，封顶 9。"""
    if a <= 0:
        return 0
    return min(int(math.log10(a)) + 2, 9)


def _utxo_bucket(u: float) -> int:
    if u <= 0:
        return 0
    return min(int(math.log10(u)) + 1, 6)


def _hkey(s: str) -> str:
    return hashlib.sha1(str(s).encode()).hexdigest()[:15]


def _addr_label(n: dict) -> str:
    recv = _num(n.get("total_received_btc"))
    sent = _num(n.get("total_sent_btc"))
    dom = 1 if (sent > recv * 1.05 or recv > sent * 1.05) else 0
    return (f"address|L{_int(n.get('first_layer'))}"
            f"|U{_utxo_bucket(_num(n.get('utxo_count')))}|D{dom}")


def _tx_labels(edges: list[dict]) -> dict[str, str]:
    """txid → tx 节点标签（输入/输出数 log 封顶 19）。"""
    io: dict[str, tuple[int, int]] = {}
    for e in edges:
        txid = e.get("txid")
        if not txid:
            continue
        ni = min(_int(e.get("total_num_inputs")), 19)
        no = min(_int(e.get("total_num_outputs")), 19)
        prev = io.get(txid)
        # 多条边共享同一 txid：优先非零 I/O（旧数据行可能缺字段）
        if prev is None or (prev == (0, 0) and (ni, no) != (0, 0)):
            io[txid] = (ni, no)
    return {f"tx:{txid}": f"tx|I{ni}|O{no}" for txid, (ni, no) in io.items()}


def _adjacency(edges: list[dict]):
    """方向感知邻接：nout/nin 分开维护（基准 WL 的方向感知核心）。"""
    nout: dict[str, list[str]] = defaultdict(list)
    nin: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        s, t = e.get("source", ""), e.get("target", "")
        if not s or not t:
            continue
        nout[s].append(t)
        nin[t].append(s)
    return nout, nin


def _payload(nid: str, nout, nin, labels: dict[str, str]) -> str:
    no_ = ",".join(sorted(labels.get(t, "?") for t in nout.get(nid, ())))
    ni_ = ",".join(sorted(labels.get(t, "?") for t in nin.get(nid, ())))
    return f"{labels.get(nid, '?')}|O[{no_}]|I[{ni_}]"


def _wl_multilists(nodes: list[dict], edges: list[dict]) -> list[list[tuple[str, int]]]:
    """各轮子树标签多重集 → 排序 [hash, count]（ctri 同构，可 JSON 序列化）。"""
    labels: dict[str, str] = {}
    for n in nodes:
        if n.get("kind") == "transaction":
            continue  # tx 标签来自边属性（canonical tx 节点无 I/O 字段）
        labels[n.get("id", "")] = _addr_label(n)
    labels.update(_tx_labels(edges))
    for e in edges:  # 兜底：tx 节点缺边属性（旧数据行）时按 id 占位
        for end in (e.get("source"), e.get("target")):
            if end and end.startswith("tx:") and end not in labels:
                labels[end] = "tx|I0|O0"
    nout, nin = _adjacency(edges)
    counters = []
    cur = dict(labels)
    for i in range(WL_ITERS):
        c: Counter = Counter()
        for nid in labels:
            c[_payload(nid, nout, nin, cur)] += 1
        counters.append(c)
        if i < WL_ITERS - 1:
            cur = {nid: hashlib.sha1(
                _payload(nid, nout, nin, cur).encode()).hexdigest()[:16]
                for nid in cur}
    return [[(_hkey(k), v) for k, v in sorted(c.items())] for c in counters]


def _fp_parts(nodes: list[dict], edges: list[dict]) -> tuple[Counter, Counter, Counter, list[int]]:
    by_id = {n.get("id"): n for n in nodes}
    m_amt: Counter = Counter()
    m_recv: Counter = Counter()
    m_utxo: Counter = Counter()
    for e in edges:
        # tx→addr 边携带该笔输出的真实金额；addr→tx 边的 dst_value
        # 是交易总量，重复计会翻倍
        if str(e.get("source", "")).startswith("tx:"):
            v = _num(e.get("dst_value_btc"))
            if v > 0:
                m_amt[round(math.log10(1 + v), 1)] += 1
    n_addr = 0
    for n in nodes:
        if n.get("kind") != "address":
            continue
        n_addr += 1
        m_recv[_amt_bucket(_num(n.get("total_received_btc")))] += 1
        m_utxo[_utxo_bucket(_num(n.get("utxo_count")))] += 1
    cv = [0] * 9
    n_stopped = n_cross = 0
    for e in edges:
        if e.get("is_stopped_expansion"):
            # 基准口径：计在 min(src 地址层 + 1, 4)
            src = by_id.get(e.get("source"), {})
            cv[min(_int(src.get("first_layer")) + 1, 4)] += 1
            n_stopped += 1
        if e.get("is_crosschain"):
            n_cross += 1
    cv[5] = len(nodes)
    cv[6] = len(edges)
    cv[7] = n_stopped
    cv[8] = n_cross
    return m_amt, m_recv, m_utxo, cv


def build_fingerprint(nodes: list[dict], edges: list[dict]) -> dict:
    """检索指纹：wljac/fp/ov 三通道的全部输入，JSON 可序列化。

    keys：wl（4 轮 [hash,count] 多重集）/ amt / recv / utxo（multiset dict，
    str keys）/ cv（9 维计数向量）/ addrs（排序地址标签）/ n_nodes / n_addr
    """
    m_amt, m_recv, m_utxo, cv = _fp_parts(nodes, edges)
    addr_labels = sorted(n.get("label") for n in nodes
                         if n.get("kind") == "address" and n.get("label"))
    return {
        "wl": _wl_multilists(nodes, edges),
        "amt": {str(k): v for k, v in sorted(m_amt.items())},
        "recv": {str(k): v for k, v in sorted(m_recv.items())},
        "utxo": {str(k): v for k, v in sorted(m_utxo.items())},
        "cv": cv,
        "addrs": addr_labels,
        "n_nodes": len(nodes),
        "n_addr": len(addr_labels),
    }


def _multiset_jaccard(a, b) -> float:
    da = dict(a) if not isinstance(a, dict) else a
    db = dict(b) if not isinstance(b, dict) else b
    inter = sum(min(v, db.get(k, 0)) for k, v in da.items())
    union = sum(da.values()) + sum(db.values()) - inter
    return inter / union if union else 0.0


def wl_score(qf: dict, cf: dict) -> float:
    """方向感知 WL 子树标签 Jaccard（轮权递增）。"""
    qw, cw = qf["wl"], cf["wl"]
    total = 0.0
    for i in range(WL_ITERS):
        total += WL_ROUND_WEIGHTS[i] * _multiset_jaccard(qw[i], cw[i])
    return total / sum(WL_ROUND_WEIGHTS)


def fp_score(qf: dict, cf: dict) -> float:
    """金额/收款/UTXO 多重集 Jaccard + 计数向量亲近度（各占 1/4）。"""
    cv_sim = 1.0 - sum(abs(a - b) for a, b in zip(qf["cv"], cf["cv"])) / max(
        sum(qf["cv"]) + sum(cf["cv"]), 1)
    return (_multiset_jaccard(qf["amt"], cf["amt"])
            + _multiset_jaccard(qf["recv"], cf["recv"])
            + _multiset_jaccard(qf["utxo"], cf["utxo"])
            + cv_sim) / 4.0


def ov_score(qf: dict, cf: dict) -> float:
    """地址成员重叠 |q∩c| / min(|q|,|c|)（软包含，±20% 剪叶稳定）。"""
    qs = set(qf["addrs"])
    cs = set(cf["addrs"])
    if not qs and not cs:
        return 1.0
    if not qs or not cs:
        return 0.0
    return len(qs & cs) / max(min(len(qs), len(cs)), 1)


def scores_from_fingerprints(qf: dict, cf: dict) -> dict:
    return {"wljac": wl_score(qf, cf), "fp": fp_score(qf, cf),
            "ov": ov_score(qf, cf)}


def channel_scores(query: dict, cand: dict, iterations: int = 4) -> dict:
    """canonical dict 入口（build_fingerprint 两侧 → 三通道分数）。"""
    qn, qe = query.get("nodes") or [], query.get("edges") or []
    cn, ce = cand.get("nodes") or [], cand.get("edges") or []
    if not qn and not cn:
        return {"wljac": 1.0, "fp": 1.0, "ov": 1.0}
    if not qn or not cn:
        return {"wljac": 0.0, "fp": 0.0, "ov": 0.0}
    return scores_from_fingerprints(build_fingerprint(qn, qe),
                                    build_fingerprint(cn, ce))
