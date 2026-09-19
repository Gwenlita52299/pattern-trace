"""issue #80 建图阶段请求/时长预算测试。

- LiveEsploraProvider._fetch 在缓存未命中处计数，超请求预算抛
  BudgetExhaustedError（HTTP 尚未发起）
- 缓存命中不计数；已 exhausted 后连缓存路径也短路拒绝
- 时长预算从 provider 创建时刻计
- BFS 主循环轮询 provider.budget_exhausted 提前收敛：清空待展开分支、
  置 degraded、source_errors 带 BUDGET_EXHAUSTED
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from backend.graph_builder.builder import GraphBuilder, _esplora_error_code
from backend.graph_builder.data_source import BudgetExhaustedError, LiveEsploraProvider


def _budget_provider(budget: int | None, cached=None,
                     time_budget: float | None = None) -> LiveEsploraProvider:
    p = LiveEsploraProvider("https://mempool.space/api",
                            request_budget=budget,
                            time_budget_seconds=time_budget)
    p._http_with_fault_tolerance = lambda path: {"path": path}
    if cached is not None:
        p._redis_get = lambda key: cached
    return p


class TestRequestBudget:
    def test_exhausted_raises_before_http(self):
        p = _budget_provider(2)
        p._fetch("/a")
        p._fetch("/b")
        assert p.requests_used == 2
        with pytest.raises(BudgetExhaustedError):
            p._fetch("/c")
        assert p.budget_exhausted is True
        # 第 4 次连 HTTP 都不再尝试（requests_used 不再增长）
        with pytest.raises(BudgetExhaustedError):
            p._fetch("/d")
        assert p.requests_used == 2

    def test_cache_hits_do_not_count(self):
        p = _budget_provider(1, cached={"tx": 1})
        for _ in range(5):
            assert p._fetch("/cached") == {"tx": 1}
        assert p.requests_used == 0
        assert p.budget_exhausted is False

    def test_unlimited_when_budget_zero_or_none(self):
        for budget in (0, None):
            p = _budget_provider(budget)
            for i in range(3):
                p._fetch(f"/p{i}")
            assert p.budget_exhausted is False


class TestTimeBudget:
    def test_time_exhausted(self):
        p = _budget_provider(None, time_budget=3600.0)
        # 把起点拨回过去，避免真实 sleep
        p._started_at = time.monotonic() - 7200.0
        with pytest.raises(BudgetExhaustedError) as ei:
            p._fetch("/a")
        assert "time budget" in str(ei.value)


class TestBuilderConvergence:
    def test_budget_exhausted_error_code_mapping(self):
        assert _esplora_error_code(BudgetExhaustedError("x")) == "BUDGET_EXHAUSTED"
        assert _esplora_error_code(ValueError("x")) == "UPSTREAM_ERROR"

    def test_provider_exhausted_flag_stops_bfs_with_degraded(self):
        """provider 置 exhausted 后 BFS 提前收敛：degraded + BUDGET_EXHAUSTED。"""

        class ExhaustedLiveLike:
            budget_exhausted = True
            budget_exhausted_reason = "request budget exhausted: 600>=600"

            def address_txs(self, address):
                return [SimpleNamespace(
                    txid="t1", inputs=[],
                    outputs=[{"address": "bc1qseed", "value": 1.0}],
                    block_time=1700000000.0, unspent_outputs={"t1:0"})]

            def __call__(self, address):
                return self.address_txs(address)

        result = GraphBuilder().build("bc1qseed", ExhaustedLiveLike(), hops=2)
        assert result.stats.degraded is True
        assert any(e.get("error_code") == "BUDGET_EXHAUSTED"
                   for e in result.stats.source_errors)
        # 队列非空收敛（非自然收敛）：queue_empty=False 留真值
        assert result.stats.queue_empty is False

    def test_time_budget_in_builder_loop(self):
        """builder 侧时长预算：start 回拨即触发，不依赖 provider 元数据。"""

        class SlowLiveLike:
            def address_txs(self, address):
                time.sleep(0.005)  # 种子枚举耗时超过 1ms 预算
                return [SimpleNamespace(
                    txid="t1", inputs=[],
                    outputs=[{"address": "bc1qseed", "value": 1.0}],
                    block_time=1700000000.0, unspent_outputs={"t1:0"})]

            def __call__(self, address):
                return self.address_txs(address)

        b = GraphBuilder(build_time_budget_seconds=0.001)
        result = b.build("bc1qseed", SlowLiveLike(), hops=2,
                         seed_block_time=1700000000.0)
        assert any(e.get("error_code") == "BUDGET_EXHAUSTED"
                   for e in result.stats.source_errors)

    def test_healthy_provider_unaffected(self):
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
