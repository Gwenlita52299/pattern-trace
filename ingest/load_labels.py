"""标签表加载 — ingest-spec §4 / IG-07。

数据源：
- results/step2_label/coinjoin_outputs_labeled.parquet → addresses_meta（coinjoin 产出地址）

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

from pathlib import Path

import pyarrow.parquet as pq

COINJOIN_OUTPUTS_PARQUET = "results/step2_label/coinjoin_outputs_labeled.parquet"


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
    table = pq.read_table(base / COINJOIN_OUTPUTS_PARQUET)
    addr_rows = []
    for addr in {a.strip() for a in table.column("address").to_pylist() if a}:
        addr_rows.append({"address": addr, "labels": ["coinjoin"],
                          "source": "step2_label"})
    counts["addresses_meta"] = _upsert(
        session, _model("AddressMeta"), addr_rows, ["address"])

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
