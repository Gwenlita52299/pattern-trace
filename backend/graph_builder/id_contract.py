"""全局 ID 规范 D3 — addr:<address> / tx:<txid> / edge:<src>-><dst>."""
from __future__ import annotations

ADDR_PREFIX = "addr:"
TX_PREFIX = "tx:"
EDGE_PREFIX = "edge:"


def node_id(kind: str, value: str) -> str:
    prefix_map = {"address": ADDR_PREFIX, "transaction": TX_PREFIX}
    if kind not in prefix_map:
        raise ValueError(f"unknown node kind: {kind!r}")
    return f"{prefix_map[kind]}{value}"


def edge_id(src_id: str, dst_id: str) -> str:
    if not src_id or not dst_id:
        raise ValueError("edge endpoints must be non-empty")
    return f"{EDGE_PREFIX}{src_id}->{dst_id}"
