"""标签三表加载 — ingest-spec §4 / IG-07。

数据源（bybit_rust 基线沉淀，全部为真实数据）：
- step1_coinjoin/coinjoin_txids.csv        → coinjoin_txids（Wasabi 特征聚类产出）
- data/op_return_decoded/op_returns_interesting.csv → crosschain_tx_set（txid→协议映射）
  基线口径（step3_sub1_preprocessing）：interesting OP_RETURN 全部计入 crosschain 集合
- results/step2_label/coinjoin_outputs_labeled.parquet → addresses_meta（coinjoin 产出地址）

CSV 含 NUL 字节，读取前统一剥离。
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import pyarrow.parquet as pq

COINJOIN_CSV = "results/step1_coinjoin/coinjoin_txids.csv"
OP_RETURN_CSV = "data/op_return_decoded/op_returns_interesting.csv"
COINJOIN_OUTPUTS_PARQUET = "results/step2_label/coinjoin_outputs_labeled.parquet"


def _read_csv_nul_safe(path: Path) -> list[dict]:
    raw = path.read_bytes().replace(b"\x00", b"")
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8", errors="replace"))))


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

    # --- CoinJoin txids ---
    cj_rows = [
        {"txid": r["txid"].strip(), "coordinator": "wasabi",
         "source": "step1_coinjoin"}
        for r in _read_csv_nul_safe(base / COINJOIN_CSV)
        if r.get("txid", "").strip()
    ]
    counts["coinjoin_txids"] = _upsert(
        session, _model("CoinjoinTxid"), cj_rows, ["txid"])

    # --- crosschain txid→protocol（基线口径：interesting OP_RETURN 全集）---
    seen_pairs: set[tuple[str, str]] = set()
    cc_rows = []
    for r in _read_csv_nul_safe(base / OP_RETURN_CSV):
        txid = (r.get("txid") or "").strip()
        protocol = (r.get("protocol") or "").strip()
        pair = (txid, protocol)
        if not txid or not protocol or pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        cc_rows.append({"txid": txid, "protocol": protocol,
                        "source": "op_return_decoded"})
    counts["crosschain_tx_set"] = _upsert(
        session, _model("CrosschainTx"), cc_rows, ["txid", "protocol"])

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


def load_into_memory(session) -> tuple[set[str], set[str], dict[str, str]]:
    """加载标签集合供 GraphBuilder 使用（IG-07 最后一条预期）。

    返回 (mixer 地址集, coinjoin txid 集, crosschain txid→协议映射)。
    """
    from backend.models.knowledge import AddressMeta, CoinjoinTxid, CrosschainTx

    mixer = {row.address for row in session.query(AddressMeta)
             if set(row.labels or ()) & {"mixer", "coinjoin"}}
    cj_txids = {row.txid for row in session.query(CoinjoinTxid)}
    # 同一 txid 多协议时取字典序最小，保证确定性
    crosschain: dict[str, str] = {}
    for row in session.query(CrosschainTx).order_by(CrosschainTx.txid, CrosschainTx.protocol):
        crosschain.setdefault(row.txid, row.protocol)
    return mixer, cj_txids, crosschain


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
        mixer, cj, cc = load_into_memory(s)
    print(f"labels loaded: {counts}")
    print(f"in-memory: {len(mixer)} mixer addrs, {len(cj)} coinjoin txids, "
          f"{len(cc)} crosschain txids")
