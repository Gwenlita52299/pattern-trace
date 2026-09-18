"""Graphormer 检索向量 — unseen-similarity 基准 cos 通道的查询侧实现。

数据来源 ingest/seed/graphormer_v2/derived/：
- graphormer_pooled.npz：9,346 个 confirmed 候选的 784 维入库向量
  （[768d pool | 16 标量 z]，全库 StandardScaler、L2 归一化）+ scal μ/σ
- ego_members.npz：候选成员 union（12,643 地址）的 768d ego 向量

查询向量构造与基准 run_round 一致：
    q_pool = Σ w_i·ego[a_i] / Σ w_i,  w_i = 1/(1+depth_i)
    q_scal = scal_features(canonical, scale=1.0)
    q = [q_pool | (q_scal-mu)/sd] → L2 归一化

成员 ego 覆盖率不足时返回 None——调用方回退到旧 hybrid 召回路径
（live 场景的新地址不在 ego 语料中，静默给零向量会污染排序）。
"""
from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

DERIVED_DIR = Path(__file__).resolve().parents[2] / "ingest/seed/graphormer_v2/derived"

# 覆盖率下限：成员地址在 ego 语料中的占比低于此值时放弃 graphormer 查询向量
MIN_EGO_COVERAGE_RATIO = 0.5


@lru_cache(maxsize=1)
def _pooled():
    try:
        d = np.load(str(DERIVED_DIR / "graphormer_pooled.npz"))
    except (FileNotFoundError, OSError) as exc:
        # issue #78：npz 缺失（全新环境/精简镜像）不得 500——留可 grep 的
        # 降级标记，调用方回退 hybrid 召回（与覆盖率不足同级）
        log.warning("[graphormer] pooled vectors unavailable (%s); "
                    "graphormer channel disabled, falling back to hybrid", exc)
        return None
    return d["pooled"], d["seeds"], d["scal_mu"], d["scal_sd"]


@lru_cache(maxsize=1)
def ego_lookup() -> dict[str, np.ndarray]:
    try:
        d = np.load(str(DERIVED_DIR / "ego_members.npz"))
    except (FileNotFoundError, OSError) as exc:
        log.warning("[graphormer] ego members unavailable (%s); "
                    "graphormer channel disabled, falling back to hybrid", exc)
        return {}
    return {a: v for a, v in zip(d["addrs"].tolist(), d["ego"])}


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


def scal_features_from_canonical(canon: dict) -> np.ndarray:
    """基准 scal_features(16 维) 的 canonical 版本（scale=1.0）。

    基准口径：层级边计数 log10、layer1 金额总量 log10、layer1 等额占比、
    跨链/悬挂比例、深度均值、成员 recv/sent/utxo 聚合、censored 占比。
    """
    edges = canon.get("edges") or []
    nodes = canon.get("nodes") or []
    layer_of = {n.get("id"): _int(n.get("first_layer")) for n in nodes}
    tx_src = {e.get("txid"): e.get("source") for e in edges
              if str(e.get("source", "")).startswith("addr:")}

    pe = defaultdict(list)
    for e in edges:
        s, t = e.get("source", ""), e.get("target", "")
        if str(s).startswith("tx:"):
            lvl = layer_of.get(t, -1)  # tx→addr：dst 地址所在层
            if 1 <= lvl <= 4:
                pe[lvl].append(_num(e.get("dst_value_btc")))
            elif lvl <= 0:  # stopped/悬挂：计在源地址下一层
                src_layer = layer_of.get(tx_src.get(e.get("txid")), 0)
                pe[min(src_layer + 1, 4)].append(0.0)
    all_amounts = [v for lst in pe.values() for v in lst]
    n_edges = len(all_amounts)
    lvl1 = pe[1]
    n1 = len(lvl1)
    tot_v1 = sum(lvl1)
    vals1 = Counter(round(v, 8) for v in lvl1)
    eq1 = (max(vals1.values()) / n1) if n1 else 0.0

    addr_nodes = [n for n in nodes if n.get("kind") == "address"]
    depths = [max(_int(n.get("first_layer")), 0) for n in addr_nodes]
    n_cross = sum(1 for e in edges if e.get("is_crosschain"))
    return np.array([
        np.log10(1 + len(addr_nodes)),
        np.log10(1 + n_edges),
        np.log10(1 + len(pe[1])),
        np.log10(1 + len(pe[2])),
        np.log10(1 + len(pe[3])),
        np.log10(1 + len(pe[4])),
        np.log10(1 + tot_v1),
        eq1,
        n_cross / n_edges if n_edges else 0.0,
        0.0,
        0.0,  # dangling 比例：canonical 引用完整，无真正悬挂边
        float(np.mean(depths)) if depths else 0.0,
        np.log10(1 + sum(_num(n.get("total_received_btc")) for n in addr_nodes)),
        np.log10(1 + sum(_num(n.get("total_sent_btc")) for n in addr_nodes)),
        np.log10(1 + float(np.mean([_num(n.get("utxo_count"))
                                    for n in addr_nodes])) if addr_nodes else 0.0),
        (sum(1 for n in addr_nodes if n.get("is_censored")) / len(addr_nodes))
        if addr_nodes else 0.0,
    ], np.float32)


def graphormer_query_vector(canon: dict) -> np.ndarray | None:
    """canonical 子图 → 784 维查询向量；数据缺失/ego 覆盖不足时 None。"""
    pooled = _pooled()
    if pooled is None:
        return None
    _, _, scal_mu, scal_sd = pooled
    ego = ego_lookup()
    addr_nodes = [n for n in (canon.get("nodes") or [])
                  if n.get("kind") == "address"]
    if not addr_nodes:
        return None
    covered = [n for n in addr_nodes if n.get("label") in ego]
    coverage = len(covered) / len(addr_nodes)
    if coverage < MIN_EGO_COVERAGE_RATIO:
        return None
    w = np.array([1.0 / (1.0 + max(_int(n.get("first_layer")), 0))
                  for n in covered], np.float32)[:, None]
    q_pool = (w * np.vstack([ego[n["label"]] for n in covered])).sum(axis=0) / w.sum()
    q_scal = scal_features_from_canonical(canon)
    q = np.concatenate([q_pool, (q_scal - scal_mu) / scal_sd])
    q /= max(float(np.linalg.norm(q)), 1e-9)
    return q.astype(np.float32)


def index_vectors() -> tuple[np.ndarray, dict[str, int]]:
    """入库向量矩阵 + seed_address → 行号映射（离线评估/校验用）。"""
    pooled = _pooled()
    if pooled is None:
        raise FileNotFoundError(
            f"graphormer pooled vectors not found under {DERIVED_DIR}; "
            "run ingest/load_graphormer_candidates.py first")
    pooled, seeds, _, _ = pooled
    return pooled, {s: i for i, s in enumerate(seeds.tolist())}
