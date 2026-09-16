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

cos 通道 = stage1 混合召回分（结构+语义 cosine），由 retriever 直接取用。
align 通道需要节点级 embedding 语料（基准用 graphormer ego embedding），
生产侧未接入：权重 >0 时显式报错，防止静默降级为全 0 通道。
"""
from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict

WL_ROUND_WEIGHTS = (0.1, 0.2, 0.3, 0.4)


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


def _multiset_jaccard(a: Counter, b: Counter) -> float:
    inter = sum((a & b).values())
    union = sum((a | b).values())
    return inter / union if union else 0.0


def _sorted_ctri(counter: Counter) -> list[tuple[str, int]]:
    return sorted(counter.items())


def _ajac(a: list[tuple[str, int]], b: list[tuple[str, int]]) -> float:
    da, db = dict(a), dict(b)
    inter = sum(min(v, db.get(k, 0)) for k, v in da.items())
    union = sum(da.values()) + sum(db.values()) - inter
    return inter / union if union else 0.0


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


def _wl_counters(nodes: list[dict], edges: list[dict],
                 iterations: int) -> list[Counter]:
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
    for i in range(iterations):
        c: Counter = Counter()
        for nid in labels:
            c[_payload(nid, nout, nin, cur)] += 1
        counters.append(c)
        if i < iterations - 1:
            cur = {nid: hashlib.sha1(
                _payload(nid, nout, nin, cur).encode()).hexdigest()[:16]
                for nid in cur}
    return counters


def wl_channel(query: dict, cand: dict, iterations: int = 4) -> float:
    """方向感知 WL 子树标签 Jaccard（轮权递增）。"""
    qn, qe = query.get("nodes") or [], query.get("edges") or []
    cn, ce = cand.get("nodes") or [], cand.get("edges") or []
    if not qn and not cn:
        return 1.0
    if not qn or not cn:
        return 0.0
    iters = min(iterations, len(WL_ROUND_WEIGHTS))
    qc = _wl_counters(qn, qe, iters)
    cc = _wl_counters(cn, ce, iters)
    total = 0.0
    for i in range(iters):
        total += WL_ROUND_WEIGHTS[i] * _multiset_jaccard(qc[i], cc[i])
    return total / sum(WL_ROUND_WEIGHTS[:iters])


def _count_vector(canon: dict) -> list[int]:
    """基准 fp 的 9 维计数向量：[stop_l1..4, 节点数, 边数, stop 总数, 跨链数]。"""
    edges = canon.get("edges") or []
    nodes = canon.get("nodes") or []
    by_id = {n.get("id"): n for n in nodes}
    cv = [0] * 9
    n_stopped = n_cross = 0
    for e in edges:
        if e.get("is_stopped_expansion"):
            src = by_id.get(e.get("source"), {})
            cv[min(_int(src.get("first_layer")) + 1, 4)] += 1
            n_stopped += 1
        if e.get("is_crosschain"):
            n_cross += 1
    cv[5] = len(nodes)
    cv[6] = len(edges)
    cv[7] = n_stopped
    cv[8] = n_cross
    return cv


def _fp_parts(canon: dict):
    edges = canon.get("edges") or []
    nodes = canon.get("nodes") or []
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
    for n in nodes:
        if n.get("kind") != "address":
            continue
        m_recv[_amt_bucket(_num(n.get("total_received_btc")))] += 1
        m_utxo[_utxo_bucket(_num(n.get("utxo_count")))] += 1
    return m_amt, m_recv, m_utxo


def fp_channel(query: dict, cand: dict) -> float:
    """金额/收款/UTXO 多重集 Jaccard + 计数向量亲近度（各占 1/4）。"""
    q_amt, q_recv, q_utxo = _fp_parts(query)
    c_amt, c_recv, c_utxo = _fp_parts(cand)
    qcv, ccv = _count_vector(query), _count_vector(cand)
    qsum, csum = sum(qcv), sum(ccv)
    cv_sim = 1.0 - sum(abs(a - b) for a, b in zip(qcv, ccv)) / max(qsum + csum, 1)
    return (_ajac(_sorted_ctri(q_amt), _sorted_ctri(c_amt))
            + _ajac(_sorted_ctri(q_recv), _sorted_ctri(c_recv))
            + _ajac(_sorted_ctri(q_utxo), _sorted_ctri(c_utxo))
            + cv_sim) / 4.0


def ov_channel(query: dict, cand: dict) -> float:
    """地址成员重叠 |q∩c| / min(|q|,|c|)（软包含，±20% 剪叶稳定）。"""
    def addr_labels(canon):
        return {n.get("label") for n in (canon.get("nodes") or [])
                if n.get("kind") == "address" and n.get("label")}

    qs, cs = addr_labels(query), addr_labels(cand)
    if not qs and not cs:
        return 1.0
    if not qs or not cs:
        return 0.0
    return len(qs & cs) / max(min(len(qs), len(cs)), 1)


def channel_scores(query: dict, cand: dict, iterations: int = 4) -> dict:
    """一次算齐三个结构通道（rerank 内循环调用）。"""
    return {
        "wljac": wl_channel(query, cand, iterations=iterations),
        "fp": fp_channel(query, cand),
        "ov": ov_channel(query, cand),
    }
