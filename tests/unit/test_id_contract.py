"""全局 ID 规范 D3 — graph-builder-spec."""
import pytest

from backend.graph_builder.id_contract import edge_id, node_id


class TestNodeID:
    def test_address_node(self):
        assert node_id("address", "bc1qxx") == "addr:bc1qxx"

    def test_transaction_node(self):
        assert node_id("transaction", "abc123") == "tx:abc123"

    def test_invalid_kind_raises(self):
        with pytest.raises(ValueError, match="unknown node kind"):
            node_id("utxo", "x")


class TestEdgeID:
    def test_edge_format(self):
        assert edge_id("addr:a", "tx:b") == "edge:addr:a->tx:b"

    @pytest.mark.parametrize("src,dst", [("", "tx:b"), ("addr:a", "")])
    def test_empty_endpoint_raises(self, src, dst):
        with pytest.raises(ValueError, match="non-empty"):
            edge_id(src, dst)
