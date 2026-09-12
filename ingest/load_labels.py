"""标签表加载 — ingest-spec §4 / IG-07。

数据源：
- results/step2_label/coinjoin_outputs_labeled.parquet → addresses_meta（coinjoin 产出地址）
- ingest/seed/lazarus_btc_stolen_addresses.csv → addresses_meta（lazarus 标签，
  作为子图过滤的接触证据与 direct_related_to_lazarus 回填依据）

配套说明：
- CoinJoin 交易**不再由外部 csv（step1_coinjoin/coinjoin_txids.csv）供给**，
  改由 backend/detection/coinjoin.py 的启发式规则在 GraphBuilder 阶段直接按交易结构
  判定 `early_stop_wasabi`；本模块不再读取该 csv 文件（移除 csv 依赖）。
- 跨链协议判定（issue #5）**不再由 CSV / crosschain_tx_set 标签表供给**，改由
  backend/detection/crosschain.py::CrosschainDetector 在运行时按 Esplora 交易字段
  （OP_RETURN / pegout）直接判定。本模块不再读取 op_returns_interesting.csv，
  也不再写入 crosschain_tx_set 表（该表与 ORM 模型已删除）。
"""
from __future__ import annotations

import csv
from pathlib import Path

import pyarrow.parquet as pq

COINJOIN_OUTPUTS_PARQUET = "results/step2_label/coinjoin_outputs_labeled.parquet"

# seed 目录随仓库走（gitignore 内），不挂在 lazarus_data_dir 下；
# 缺文件时跳过（CI / 无 seed 数据环境不阻塞标签加载）
LAZARUS_LABELS_CSV = Path(__file__).resolve().parent / "seed" / \
    "lazarus_btc_stolen_addresses.csv"


def parse_lazarus_csv(path: Path) -> list[dict]:
    """解析 Lazarus 被盗地址 CSV → AddressMeta 行（纯函数，便于单测）。"""
    if not path.exists():
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            addr = (raw.get("address") or "").strip()
            if not addr:
                continue
            rows.append({"address": addr, "labels": ["lazarus"],
                         "source": "lazarus_stolen_csv"})
    return rows


def _merge_with_existing(session, model, rows: list[dict]) -> list[dict]:
    """与库中已有 labels 求并集——lazarus upsert 不能覆盖同地址的 coinjoin 标签。"""
    if not rows:
        return rows
    addrs = [r["address"] for r in rows]
    existing = {r.address: set(r.labels or ())
                for r in session.query(model).filter(model.address.in_(addrs))}
    for r in rows:
        r["labels"] = sorted(existing.get(r["address"], set()) | set(r["labels"]))
    return rows


def _upsert(session, model, rows: list[dict], key_cols: list[str]) -> int:
    try:
        from .common import upsert_rows
    except ImportError:
        from common import upsert_rows

    return upsert_rows(session, model, rows, key_cols)


def run(session, base_dir: str | None = None) -> dict[str, int]:
    from backend.core.config import get_settings

    base = Path(base_dir or get_settings().lazarus_data_dir)
    counts: dict[str, int] = {}

    # --- CoinJoin 产出地址 → addresses_meta ---
    # 同样走 label 合并：重跑时不得覆盖同地址上已合并的 lazarus 标签
    table = pq.read_table(base / COINJOIN_OUTPUTS_PARQUET)
    addr_rows = []
    for addr in {a.strip() for a in table.column("address").to_pylist() if a}:
        addr_rows.append({"address": addr, "labels": ["coinjoin"],
                          "source": "step2_label"})
    addr_rows = _merge_with_existing(session, _model("AddressMeta"), addr_rows)
    counts["addresses_meta"] = _upsert(
        session, _model("AddressMeta"), addr_rows, ["address"])

    # --- Lazarus 被盗地址 → addresses_meta（labels=["lazarus"]）---
    lazarus_rows = _merge_with_existing(
        session, _model("AddressMeta"), parse_lazarus_csv(LAZARUS_LABELS_CSV))
    counts["lazarus_labels"] = _upsert(
        session, _model("AddressMeta"), lazarus_rows, ["address"])

    return counts


def _model(name: str):
    import backend.models.knowledge as k

    return getattr(k, name)


def load_into_memory(session) -> tuple[set[str], set[str]]:
    """加载标签集合供 GraphBuilder 使用（IG-07 最后一条预期）。

    返回 (mixer 地址集, coinjoin txid 集)。跨链判定由运行时 CrosschainDetector 承担，
    不再从 DB 标签表加载 txid→protocol 映射（crosschain_tx_set 已移除）。
    """
    from backend.models.knowledge import AddressMeta, CoinjoinTxid

    mixer = {row.address for row in session.query(AddressMeta)
             if set(row.labels or ()) & {"mixer", "coinjoin"}}
    cj_txids = {row.txid for row in session.query(CoinjoinTxid)}
    return mixer, cj_txids


if __name__ == "__main__":
    import os
    import sys

    # 支持直接运行（IG-07 步骤写法）；包内运行时无需此回退
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.environ.setdefault("JWT_SECRET", "migration-placeholder")

    from common import get_engine
    from sqlalchemy.orm import Session

    engine = get_engine()
    with Session(engine) as s:
        counts = run(s)
        mixer, cj = load_into_memory(s)
    print(f"labels loaded: {counts}")
    print(f"in-memory: {len(mixer)} mixer addrs, {len(cj)} coinjoin txids")
