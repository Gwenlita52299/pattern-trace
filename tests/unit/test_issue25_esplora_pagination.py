"""issue #25 live Esplora 分页拉取测试。

- 持续请求 /txs/chain/:last_seen_txid 直至末页，历史交易按序进入结果
- 重复交易（txid）跨页去重
- 页数硬上限：到达上限截断并记录 truncated_addresses
- seed_block_time 基于完整分页数据（非首屏）
- 截断经 GraphBuilder 显式置 degraded（HISTORY_TRUNCATED）
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.graph_builder.builder import GraphBuilder
from backend.graph_builder.data_source import LiveEsploraProvider


def _raw_tx(txid: str, block_time: float = 1700000000.0) -> dict:
    return {
        "txid": txid,
        "vin": [],
        "vout": [{
            "scriptpubkey_address": "bc1qdst", "value": 50000000,
            "status": {"spent": False},
        }],
        "status": {"block_time": block_time},
    }


def _paged_provider(pages: list[list[dict]], page_size: int,
                    max_pages: int | None = None) -> LiveEsploraProvider:
    """_fetch 打桩的 live provider：pages 按请求顺序返回。"""
    p = LiveEsploraProvider("https://mempool.space/api",
                            max_pages=max_pages)
    p.PAGE_SIZE = page_size
    p.calls: list[str] = []
    queue = list(pages)

    def _fetch(path: str):
        p.calls.append(path)
        return queue.pop(0)

    p._fetch = _fetch
    return p


class TestPagination:
    def test_multi_page_chain_fetch_in_order(self):
        """满页后继续请求 /txs/chain/:last_txid，历史交易按序聚合。"""
        page1 = [_raw_tx(f"t{i}") for i in range(3)]
        page2 = [_raw_tx(f"h{i}") for i in range(1)]  # < PAGE_SIZE → 末页
        p = _paged_provider([page1, page2], page_size=3)
        txs = p.address_txs("bc1qseed")
        assert [t.txid for t in txs] == ["t0", "t1", "t2", "h0"]
        assert p.calls == ["/address/bc1qseed/txs",
                           "/address/bc1qseed/txs/chain/t2"]
        assert "bc1qseed" not in p.truncated_addresses

    def test_duplicate_txids_deduped_across_pages(self):
        """备用端点/缓存重叠页：重复 txid 只保留首次出现。"""
        page1 = [_raw_tx("t1"), _raw_tx("t2"), _raw_tx("t3")]
        page2 = [_raw_tx("t3"), _raw_tx("t4")]  # t3 与首页重叠
        p = _paged_provider([page1, page2], page_size=3)
        txs = p.address_txs("bc1qseed")
        assert [t.txid for t in txs] == ["t1", "t2", "t3", "t4"]

    def test_empty_history(self):
        p = _paged_provider([[]], page_size=3)
        assert p.address_txs("bc1qseed") == []
        assert "bc1qseed" not in p.truncated_addresses


class TestPageCap:
    def test_truncated_at_max_pages_and_recorded(self):
        """3 个满页、上限 2 页：返回前 2 页，地址进入 truncated_addresses。"""
        pages = [[_raw_tx(f"p{pg}_{i}") for i in range(3)] for pg in range(3)]
        p = _paged_provider(pages, page_size=3, max_pages=2)
        txs = p.address_txs("bc1qseed")
        assert len(txs) == 6
        assert [t.txid for t in txs] == [f"p{pg}_{i}" for pg in range(2)
                                         for i in range(3)]
        assert "bc1qseed" in p.truncated_addresses
        assert "truncated" in p.truncated_addresses["bc1qseed"]

    def test_cap_not_hit_when_last_page_short(self):
        """末页不满页即自然结束，上限 2 不产生截断标记。"""
        pages = [[_raw_tx("a"), _raw_tx("b"), _raw_tx("c")],
                 [_raw_tx("d")]]
        p = _paged_provider(pages, page_size=3, max_pages=2)
        p.address_txs("bc1qseed")
        assert "bc1qseed" not in p.truncated_addresses


class TestSeedBlockTime:
    def test_uses_full_paginated_data(self):
        """issue #25：基准取全部已获取页的 block_time 最大值。"""
        page1 = [_raw_tx("t1", block_time=100.0),
                 _raw_tx("t2", block_time=1700000000.0)]
        page2 = [_raw_tx("h1", block_time=900.0)]
        p = _paged_provider([page1, page2], page_size=2)
        assert p.seed_block_time("bc1qseed") == 1700000000.0
        # 完整分页：chain 请求已发出
        assert p.calls[-1] == "/address/bc1qseed/txs/chain/t2"

    def test_seed_truncation_still_recorded(self):
        """seed 分页触顶：基准仍取已获取数据最大值，地址标记截断。"""
        pages = [[_raw_tx(f"t{i}", block_time=100.0 + i) for i in range(2)],
                 [_raw_tx("never", block_time=999.0)]]
        p = _paged_provider(pages, page_size=2, max_pages=1)
        assert p.seed_block_time("bc1qseed") == 101.0
        assert "bc1qseed" in p.truncated_addresses


class TestBuilderDegraded:
    def test_truncated_history_marks_subgraph_degraded(self):
        """截断经 builder 显式置 degraded：子图不得伪装 complete。"""

        class TruncatedLiveLike:
            truncated_addresses = {
                "bc1qseed": "history truncated at 2 pages (~50 txs)"}

            def address_txs(self, address):
                return []

        result = GraphBuilder().build("bc1qseed", TruncatedLiveLike(), hops=1)
        assert result.stats.degraded is True
        assert result.stats.data_quality == "degraded"
        assert any(e.get("error_code") == "HISTORY_TRUNCATED"
                   for e in result.stats.source_errors)

    def test_non_truncated_provider_untouched(self):
        class NormalProvider:
            def address_txs(self, address):
                return [SimpleNamespace(
                    txid="t1", inputs=[],
                    outputs=[{"address": "bc1qseed", "value": 1.0}],
                    block_time=1700000000.0, unspent_outputs={"t1:0"})]

            def __call__(self, address):
                return self.address_txs(address)

        result = GraphBuilder().build("bc1qseed", NormalProvider(), hops=1)
        assert result.stats.degraded is False
        assert result.stats.unspent == 1
