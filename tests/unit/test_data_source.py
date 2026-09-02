"""数据源层 issue #3 判定与分页 — outspend 权威 spent_by + 地址交易分页。

不为 live provider 发公网请求：用 _fetch / _map_tx 语义注入确定性分页响应。
"""
from backend.graph_builder.builder import GraphBuilder
from backend.graph_builder.data_source import FixtureTxProvider, LiveEsploraProvider


def _raw_tx(txid: str) -> dict:
    return {
        "txid": txid,
        "vin": [{
            "txid": "prev", "vout": 0,
            "prevout": {"scriptpubkey_address": "bc1qprev", "value": 100000000},
        }],
        "vout": [{
            "scriptpubkey_address": "bc1qdst", "value": 50000000,
            "status": {"spent": False},
        }],
        "block_time": 1700000000.0,
    }


# ---------------------------------------------------------------------------
# Live 地址交易分页（/txs + /txs/chain 直至末页）
# ---------------------------------------------------------------------------
def test_live_provider_address_txs_paginates_via_chain():
    class Paged(LiveEsploraProvider):
        def __init__(self):
            super().__init__("https://mempool.space/api")
            self.calls: list[str] = []

        def _fetch(self, path):
            self.calls.append(path)
            if "/txs/chain/" in path:
                return [_raw_tx("t3")]          # 末页（1 < PAGE_SIZE=2）
            return [_raw_tx("t1"), _raw_tx("t2")]  # 首页（== PAGE_SIZE → 继续翻页）

    p = Paged()
    p.PAGE_SIZE = 2
    txs = p.address_txs("bc1qseed")
    assert [t.txid for t in txs] == ["t1", "t2", "t3"]
    assert p.calls == [
        "/address/bc1qseed/txs",
        "/address/bc1qseed/txs/chain/t2",
    ]


def test_live_provider_single_page_stops():
    class SinglePage(LiveEsploraProvider):
        def __init__(self):
            super().__init__("https://mempool.space/api")
            self.calls: list[str] = []

        def _fetch(self, path):
            self.calls.append(path)
            return [_raw_tx("t1")]  # 1 < PAGE_SIZE → 末页

    p = SinglePage()
    p.PAGE_SIZE = 2
    assert [t.txid for t in p.address_txs("a")] == ["t1"]
    assert p.calls == ["/address/a/txs"]


def test_live_provider_outspend_spent_and_unspent():
    p = LiveEsploraProvider("https://mempool.space/api")
    p._fetch = lambda path: {
        "/tx/abc/outspend/3": {"spent": True, "txid": "spend_tx", "vin": {"txid": "abc", "vout": 3}},
        "/tx/def/outspend/0": {"spent": False, "txid": None, "vin": None},
    }[path]

    spent = p.outspend("abc", 3)
    assert spent.spent is True
    assert spent.txid == "spend_tx"

    unspent = p.outspend("def", 0)
    assert unspent.spent is False
    assert unspent.txid is None


def test_live_provider_get_tx_maps_to_namespace():
    p = LiveEsploraProvider("https://mempool.space/api")
    p._fetch = lambda path: {
        "/tx/spend_tx": {
            "txid": "spend_tx",
            "vin": [{"txid": "prev", "vout": 0,
                     "prevout": {"scriptpubkey_address": "bc1qprev", "value": 100000000}}],
            "vout": [{"scriptpubkey_address": "bc1qdst", "value": 50000000,
                      "status": {"spent": False}}],
            "block_time": 1700000000.0,
        },
    }[path]

    tx = p.get_tx("spend_tx")
    assert tx.txid == "spend_tx"
    assert tx.inputs[0]["prev_txid"] == "prev"
    assert tx.outputs[0]["value"] == 0.5  # 已换算成 BTC（1e8 → 5e7/1e8）
    assert tx.block_time == 1700000000.0


def test_live_provider_reads_status_block_time_from_esplora():
    """Esplora 区块时间位于 status.block_time（未确认交易无该字段 → None）。"""
    p = LiveEsploraProvider("https://mempool.space/api")
    p._fetch = lambda path: {
        "/tx/confirmed": {
            "txid": "confirmed",
            "vin": [],
            "vout": [{"scriptpubkey_address": "bc1qdst", "value": 1,
                      "status": {"spent": True}}],
            "status": {"confirmed": True, "block_height": 800000, "block_time": 1700000100.0},
        },
        "/tx/mempool": {
            "txid": "mempool",
            "vin": [],
            "vout": [{"scriptpubkey_address": "bc1qdst", "value": 1,
                      "status": {"spent": False}}],
            "status": {"confirmed": False},
        },
    }[path]

    assert p.get_tx("confirmed").block_time == 1700000100.0
    assert p.get_tx("mempool").block_time is None  # 未确认交易无区块时间


# ---------------------------------------------------------------------------
# Fixture outspend / get_tx 语义
# ---------------------------------------------------------------------------
def test_fixture_provider_outspend_matches_spent_by_ledger():
    fp = FixtureTxProvider.load()
    assert fp._spent_by, "fixture should contain spent relationships"

    for (txid, vout), spender in list(fp._spent_by.items())[:5]:
        info = fp.outspend(txid, vout)
        assert info.spent is True
        assert info.txid == spender

    # 未在账本中的 UTXO → 权威 unspent
    info = fp.outspend("nonexistent_txid", 0)
    assert info.spent is False
    assert info.txid is None


def test_fixture_provider_resolve_spending_transaction():
    fp = FixtureTxProvider.load()
    (txid, vout), spender = next(iter(fp._spent_by.items()))
    res = fp.resolve_spending_transaction(txid, vout, owner="bc1qowner")
    assert res.spent is True
    assert res.spending_txid == spender
    assert res.spending_tx is not None
    assert res.spending_tx.txid == spender
    assert res.txid == txid and res.vout == vout and res.owner == "bc1qowner"

    un = fp.resolve_spending_transaction("nonexistent", 0, owner="bc1qowner")
    assert un.spent is False
    assert un.spending_txid is None
    assert un.spending_tx is None
    assert un.owner == "bc1qowner"


def test_live_provider_resolve_spending_transaction():
    p = LiveEsploraProvider("https://mempool.space/api")

    def fake_fetch(path):
        if path == "/tx/abc/outspend/3":
            return {"spent": True, "txid": "spend_tx", "vin": {"txid": "abc", "vout": 3}}
        if path == "/tx/spend_tx":
            return {
                "txid": "spend_tx",
                "vin": [{"txid": "abc", "vout": 3,
                         "prevout": {"scriptpubkey_address": "bc1qowner", "value": 100000000}}],
                "vout": [{"scriptpubkey_address": "bc1qdst", "value": 50000000,
                          "status": {"spent": False}}],
                "status": {"confirmed": True, "block_time": 1700000000.0},
            }
        raise KeyError(path)

    p._fetch = fake_fetch
    res = p.resolve_spending_transaction("abc", 3, owner="bc1qowner")
    assert res.spent is True
    assert res.spending_txid == "spend_tx"
    assert res.spending_tx.txid == "spend_tx"
    assert res.spending_vin == 0
    assert res.owner == "bc1qowner"

    # unspent：outspend 返回 spent=false，不拉全量
    p._fetch = lambda path: {"spent": False, "txid": None, "vin": None}
    un = p.resolve_spending_transaction("def", 0)
    assert un.spent is False
    assert un.spending_tx is None


def test_fixture_provider_get_tx_round_trip():
    fp = FixtureTxProvider.load()
    txid = next(iter(fp._tx_by_txid))
    tx = fp.get_tx(txid)
    assert tx is not None
    assert tx.txid == txid
    assert hasattr(tx, "inputs") and hasattr(tx, "outputs")
    assert fp.get_tx("unknown_txid") is None


def test_fixture_provider_build_uses_authoritative_outspend():
    """用 fixture provider（含 outspend 协议）端到端建图，权威路径生效且不降级。"""
    fp = FixtureTxProvider.load()
    seed = fp.seed_addresses[0]
    seed_time = fp.seed_block_time(seed)
    result = GraphBuilder(
        coinjoin_txids=fp.coinjoin_txids,
        crosschain_tx_set=fp.crosschain_tx_set,
    ).build(seed, fp, hops=2, time_window_days=90, seed_block_time=seed_time)
    assert result.stats.degraded is False
    assert len(result.nodes) > 1
    # 权威路径在非末页也有消费交易 → 至少一个分支正常展开或 early_stop
    assert (result.stats.expanded + result.stats.early_stop_wasabi
            + result.stats.early_stop_crosschain) > 0
