"""分析管线的 tx_provider 数据源 — graph-builder-spec §4 Esplora 接入。

双模式：
- fixture：确定性演示数据（infra/fixtures/demo_txs.json），端到端测试与
  离线演示不依赖公网 Esplora；含夹具自带的 coinjoin 标记集，而跨链终止语义
  由运行时 CrosschainDetector 在真实交易脚本字段上判别（无 CSV / 标签库），
  使演示子图呈现与真实 Lazarus 场景一致的 mixer/crosschain 终止语义。
- live：同步 httpx 访问 Esplora API（mempool.space 兼容），带重试退避/熔断/
  备用端点/Redis 缓存容错层（与 esplora.EsploraClient 共享熔断器与缓存键规范）。

builder 的 provider 契约（issue #3 统一模型）：
- 地址根枚举 `.address_txs(address)`（live 全量分页；fixture 为确定性图）
- 权威消费解析 `.outspend(txid, vout)` → {spent, txid, vin}（Esplora outspend API）
- 完整交易 `.get_tx(txid)`（Esplora /tx/:txid）

仍兼容旧的「同步可调用对象返回 tx 列表」形态（回退到地址扫描的 spent_by 解析），
供纯 callable 测试桩/基准脚本使用。EsploraClient（异步+熔断）服务于异步调用方。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from .esplora import CircuitBreaker, CircuitOpenError
from ..detection.crosschain import CrosschainDetector

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "infra" / "fixtures" / "demo_txs.json"


@dataclass
class SpendingResult:
    """领域级消费判定结果（issue #3「建议的模块边界」）。

    统一封装 outspend 判定 + 全量消费交易获取：
    - spent=false → spending_txid/spending_tx 为空（unspent 叶子）
    - spent=true  → spending_txid 为消费交易 id，spending_tx 为完整消费交易，
                    spending_vin 为消费交易中引用该 UTXO 的 input 下标（best-effort）
    同时保留 UTXO 上下文（txid/vout/owner/value/address/status）供调用方使用。
    """

    spent: bool
    spending_txid: str | None = None
    spending_vin: int | None = None
    spending_tx: object | None = None
    # UTXO 上下文
    txid: str = ""
    vout: int = 0
    owner: str = ""
    value: float = 0.0
    address: str = ""
    status: str = ""


def _find_spending_input_index(spending_tx, txid: str, vout: int) -> int | None:
    """在消费交易 inputs 中找到引用 (txid, vout) 的 input 下标（best-effort）。"""
    for i, inp in enumerate(getattr(spending_tx, "inputs", None) or []):
        if inp.get("prev_txid") == txid and inp.get("prev_vout") == vout:
            return i
    return None


class FixtureTxProvider:
    def __init__(self, data: dict):
        self.data = data
        self.txs_by_address: dict[str, list] = data["txs_by_address"]
        self.seed_addresses: list[str] = data.get("seed_addresses", [])
        self.coinjoin_txids: set[str] = set(data.get("coinjoin_txids", []))
        # issue #4/#5：跨链判定由运行时 CrosschainDetector 承担，不再预置
        # crosschain_tx_set 标签映射（CSV / DB 标签链路已移除）。
        self.op_return_scenarios: dict[str, dict] = data.get("op_return_scenarios", {})
        self._crosschain_detector = CrosschainDetector()
        self._input_prevouts: dict[str, list[tuple[str, int]]] = {}
        self._tx_by_txid: dict[str, dict] = {}
        # outspend 权威索引：(prev_txid, prev_vout) -> 消费它的交易 txid。
        # 与 Esplora /tx/:txid/outspend/:vout 的语义对齐（issue #3 unified model）。
        self._spent_by: dict[tuple[str, int], str] = {}
        self._build_spent_by_ledger()

    def seed_block_time(self, address: str) -> float | None:
        times = [t.get("block_time") for t in self.txs_by_address.get(address, [])
                 if t.get("block_time") is not None]
        return max(times) if times else None

    # ------------------------------------------------------------------
    # spent_by 账本：把每个 input 解析成其消费的 UTXO（prev_txid, prev_vout）。
    #
    # 基线在 parquet 上用 spent_by_index 预计算 world；(txid, vout) -> spending_txid。
    # PatternTrace 的 fixture 是确定性 DAG，可在离线按「地址先产出、后消费」的
    # 两遍法重建等价账本：先登记所有输出为该地址拥有的 UTXO，再为每个 input 弹出
    # 一个该地址拥有的 UTXO；无父输出（如种子地址的资金来源）则用外部合成 prevout。
    # 这样 builder 的 _spending_tx 才能匹配到 (owner, utxo) 的消费交易。
    # ------------------------------------------------------------------
    def _build_spent_by_ledger(self) -> None:
        from collections import defaultdict, deque

        # 只有会被展开的消费交易（非 coinjoin / 非 crosschain）的输出才会被 builder
        # 作为下一层 UTXO 入队；被 early_stop 截断的交易不会产生向下展开的子 UTXO。
        # 账本必须与 builder 的展开语义一致，否则 `(utxo, owner)` 无法匹配到消费交易。
        # 跨链判定与 builder 一致：运行时 CrosschainDetector（无 crosschain_tx_set 标签库）。
        def is_stopped(tx: dict) -> bool:
            return (tx["txid"] in self.coinjoin_txids
                    or self._crosschain_detector.is_crosschain(tx))

        received: dict[str, deque[tuple[str, int]]] = defaultdict(deque)
        for _addr, txs in self.txs_by_address.items():
            for tx in txs:
                txid = tx["txid"]
                if txid not in self._tx_by_txid:  # 每个 tx 唯一归属一个消费地址
                    self._tx_by_txid[txid] = tx
                    if is_stopped(tx):
                        continue  # early_stop 截断：其输出不会作为下一层 UTXO 被消费
                    for idx, out in enumerate(tx.get("outputs", [])):
                        a = out.get("address")
                        if not a:
                            continue  # OP_RETURN / 不可追踪输出不入账本
                        received[a].append((txid, idx))

        ext_counter: dict[str, int] = defaultdict(int)
        for _addr, txs in self.txs_by_address.items():
            for tx in txs:
                txid = tx["txid"]
                prevs: list[tuple[str, int]] = []
                for inp in tx.get("inputs", []):
                    a = inp.get("address")
                    if a and received[a]:
                        prevs.append(received[a].popleft())
                    else:
                        prevs.append((f"ext-{a}-{ext_counter[a]}", 0))
                        ext_counter[a] += 1
                self._input_prevouts[txid] = prevs
                # outspend 权威索引：每个被消费的 UTXO 归因到其消费交易
                for prev_txid, prev_vout in prevs:
                    self._spent_by[(prev_txid, prev_vout)] = txid

    def _tx_namespace(self, tx: dict) -> SimpleNamespace:
        txid = tx["txid"]
        prevouts = self._input_prevouts.get(txid, [])
        inputs = []
        for i, inp in enumerate(tx.get("inputs", [])):
            inp_with_prev = dict(inp)
            if i < len(prevouts):
                prev_txid, prev_vout = prevouts[i]
                inp_with_prev["prev_txid"] = prev_txid
                inp_with_prev["prev_vout"] = prev_vout
            inputs.append(inp_with_prev)
        return SimpleNamespace(
            txid=txid, inputs=inputs, outputs=tx.get("outputs", []),
            block_time=tx.get("block_time"),
            unspent_outputs=set(tx.get("unspent_outputs", [])),
        )

    def __call__(self, address: str) -> list:
        return [self._tx_namespace(tx) for tx in self.txs_by_address.get(address, [])]

    def address_txs(self, address: str) -> list:
        """与 live provider 对齐的地址交易枚举入口（fixture 是确定性图，无分页）。"""
        return self(address)

    def get_tx(self, txid: str) -> SimpleNamespace | None:
        """完整交易详情（issue #3 统一模型：GET /tx/:txid）。"""
        tx = self._tx_by_txid.get(txid)
        if tx is None:
            return None
        return self._tx_namespace(tx)

    def outspend(self, txid: str, vout: int) -> SimpleNamespace:
        """权威 outspend 判定（issue #3：GET /tx/:txid/outspend/:vout）。

        返回 spent + 消费交易 txid；未找到消费交易 → 视为权威未花（unspent 叶子）。
        """
        spender = self._spent_by.get((txid, vout))
        if spender is None:
            return SimpleNamespace(spent=False, txid=None, vin=None)
        return SimpleNamespace(spent=True, txid=spender, vin={"txid": txid, "vout": vout})

    def resolve_spending_transaction(self, txid: str, vout: int, owner: str | None = None) -> SpendingResult:
        """领域级消费判定（issue #3「建议的模块边界」）：本地账本 → SpendingResult。"""
        info = self.outspend(txid, vout)
        spending_tx = None
        spending_vin = None
        if info.spent and info.txid:
            spending_tx = self.get_tx(info.txid)
            spending_vin = _find_spending_input_index(spending_tx, txid, vout)
        return SpendingResult(
            spent=info.spent, spending_txid=info.txid, spending_vin=spending_vin,
            spending_tx=spending_tx, txid=txid, vout=vout, owner=owner or "",
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> FixtureTxProvider:
        return cls(json.loads(Path(path or FIXTURE_PATH).read_text()))


class LiveEsploraProvider:
    """同步容错层：重试退避 + 429 Retry-After + 熔断 + 备用端点 + Redis L2。

    与 esplora.EsploraClient（异步调用方用）共享 CircuitBreaker 类和
    esplora:{base}:{path} 缓存键规范；同步形态是 builder provider 契约决定的。
    """

    CACHE_TTL_SECONDS = 86400
    MAX_RETRIES = 3
    # /address/:addr/txs 默认页大小；返回条数 < 该值即为末页（mempool.space / blockstream 均为 25）
    PAGE_SIZE = 25
    # issue #25：高活跃地址分页硬上限（40 页 × 25 ≈ 1000 笔/地址），
    # 防止单地址无限拉取拖垮分析时延；触顶即截断并记录
    DEFAULT_MAX_PAGES = 40
    FALLBACKS = {
        "https://mempool.space/api": "https://blockstream.info/api",
        "https://blockstream.info/api": "https://mempool.space/api",
    }

    def __init__(self, base_url: str, timeout_seconds: float = 10.0,
                 redis_client=None, max_pages: int | None = None,
                 request_budget: int | None = None,
                 time_budget_seconds: float | None = None):
        self.base_url = base_url.rstrip("/")
        self.fallback_base = self.FALLBACKS.get(self.base_url)
        self.timeout_seconds = timeout_seconds
        self.breaker = CircuitBreaker()
        self.redis = redis_client
        self.max_pages = max_pages if max_pages is not None \
            else self.DEFAULT_MAX_PAGES
        self.httpx_client = None  # 惰性创建，进程内复用连接池
        # issue #25：截断记录 address -> 原因，供 builder 置 degraded
        # （provider 实例每次分析新建，无需跨实例清理）
        self.truncated_addresses: dict[str, str] = {}
        # issue #80：单次分析总预算（0/None = 不限）。计数只发生在
        # _fetch 缓存未命中后、发起 HTTP 前——缓存命中与重试退避
        # 内的重复尝试不重复计入逻辑请求数
        self.request_budget = request_budget
        self.time_budget_seconds = time_budget_seconds
        self.requests_used = 0
        self.budget_exhausted = False
        self.budget_exhausted_reason = ""
        self._started_at = time.monotonic()

    def __call__(self, address: str) -> list:
        return self.address_txs(address)

    def _paged_raw(self, address: str) -> list[dict]:
        """地址交易全量分页拉取：/address/:addr/txs +
        /address/:addr/txs/chain/:last_seen_txid（issue #3/#25）。

        - 重复交易按 txid 去重（备用端点切换 / 缓存回放可能产生重叠页）
        - 到达 max_pages 硬上限即截断并登记 truncated_addresses
        """
        raw: list[dict] = []
        seen_txids: set[str] = set()
        last_seen: str | None = None
        pages_fetched = 0
        while pages_fetched < self.max_pages:
            path = (f"/address/{address}/txs" if last_seen is None
                    else f"/address/{address}/txs/chain/{last_seen}")
            page = self._fetch(path)
            pages_fetched += 1
            if not page:
                return raw
            fresh = [tx for tx in page if tx["txid"] not in seen_txids]
            raw.extend(fresh)
            seen_txids.update(tx["txid"] for tx in fresh)
            last_seen = page[-1]["txid"]
            if len(page) < self.PAGE_SIZE:
                return raw
        # 页数预算耗尽仍未到末页 → 截断留痕（验收：记录因上限被截断）
        self.truncated_addresses[address] = (
            f"history truncated at {self.max_pages} pages "
            f"(~{len(raw)} txs)")
        return raw

    def address_txs(self, address: str) -> list:
        """地址交易全量分页拉取（issue #3：/address/:addr/txs +
        /address/:addr/txs/chain/:last_seen_txid，直至末页或页数上限），
        映射为 SimpleNamespace。

        修复「/address/:address/txs 单页可能遗漏历史输出」导致的根 UTXO 缺失问题。
        """
        return [self._map_tx(tx) for tx in self._paged_raw(address)]

    def get_tx(self, txid: str) -> SimpleNamespace:
        """单笔交易详情（issue #3：GET /tx/:txid）。"""
        return self._map_tx(self._fetch(f"/tx/{txid}"))

    def outspend(self, txid: str, vout: int) -> SimpleNamespace:
        """权威 outspend 判定（issue #3：GET /tx/:txid/outspend/:vout）。

        spent=false → unspent 叶子；spent=true → 返回消费交易 txid 供 get_tx 拉全量。
        """
        data = self._fetch(f"/tx/{txid}/outspend/{vout}")
        return SimpleNamespace(spent=bool(data.get("spent")),
                              txid=data.get("txid"),
                              vin=data.get("vin"))

    def resolve_spending_transaction(self, txid: str, vout: int, owner: str | None = None) -> SpendingResult:
        """领域级消费判定（issue #3「建议的模块边界」）：outspend + get_tx → SpendingResult。"""
        data = self._fetch(f"/tx/{txid}/outspend/{vout}")
        spent = bool(data.get("spent"))
        spending_txid = data.get("txid") if spent else None
        spending_tx = None
        spending_vin = None
        if spent and spending_txid:
            spending_tx = self.get_tx(spending_txid)
            spending_vin = _find_spending_input_index(spending_tx, txid, vout)
        return SpendingResult(
            spent=spent, spending_txid=spending_txid, spending_vin=spending_vin,
            spending_tx=spending_tx, txid=txid, vout=vout, owner=owner or "",
        )

    def seed_block_time(self, address: str) -> float | None:
        """时间窗基准（spec §3 out_of_range）：种子地址最近一次上链活动。

        issue #25：基于完整分页数据计算（此前只用首屏）。页间复用同一
        _fetch 缓存 key，GraphBuilder 先拉地址后取基准时全部命中 L2，
        不产生额外网络请求。截断时最新交易仍在首屏，基准不受影响，
        但地址会留在 truncated_addresses 供 builder 置 degraded。
        """
        times = [t.block_time for t in (self._map_tx(x)
                                        for x in self._paged_raw(address))
                 if t.block_time is not None]
        return max(times) if times else None

    def address_stats(self, address: str) -> dict:
        """地址统计单请求预检（issue #79：GET /address/:addr/stats）。

        返回含 tx_count（地址涉及交易总数）等字段。与分页枚举共享
        _fetch 的 Redis L2 缓存与重试/熔断层——预检一次 HTTP 即可
        在建图前判断活跃度，避免高活跃地址进入 BFS 后产生
        数万次 outspend/get_tx 上游请求。
        """
        return self._fetch(f"/address/{address}/stats")

    # ------------------------------------------------------------------
    def _check_budget(self) -> None:
        """issue #80：预算门。请求预算按逻辑请求数（缓存未命中）计；
        时长预算从 provider 创建时刻计（覆盖重试退避等待）。超限置
        exhausted 标记并抛错——builder 在 BFS 主循环轮询该标记提前收敛，
        各分支捕获错误后 record_error 置 degraded。"""
        if self.budget_exhausted:
            raise BudgetExhaustedError(self.budget_exhausted_reason)
        if self.request_budget is not None and self.request_budget > 0 \
                and self.requests_used >= self.request_budget:
            self.budget_exhausted = True
            self.budget_exhausted_reason = (
                f"request budget exhausted: {self.requests_used}"
                f">={self.request_budget}")
        elif self.time_budget_seconds is not None \
                and self.time_budget_seconds > 0 \
                and time.monotonic() - self._started_at >= self.time_budget_seconds:
            self.budget_exhausted = True
            self.budget_exhausted_reason = (
                f"time budget exhausted: >= {self.time_budget_seconds}s")
        if self.budget_exhausted:
            print(f"[esplora] {self.budget_exhausted_reason}")
            raise BudgetExhaustedError(self.budget_exhausted_reason)

    def _fetch(self, path: str) -> object:
        self._check_budget()
        redis_key = f"esplora:{self.base_url}:{path}"
        cached = self._redis_get(redis_key)
        if cached is not None:
            return cached
        self.requests_used += 1
        data = self._http_with_fault_tolerance(path)
        self._redis_set(redis_key, data)
        return data

    def _http_with_fault_tolerance(self, path: str) -> object:
        import random
        import time as _time

        effective = self.base_url
        delay = 1.0
        last_exc: Exception | None = None
        retry_after: float | None = None
        for attempt in range(self.MAX_RETRIES):
            try:
                self.breaker.before_request()
            except CircuitOpenError:
                if self.fallback_base is None or effective != self.base_url:
                    raise
                effective = self.fallback_base  # 熔断切换备用端点（GB-18 同语义）
            try:
                data = self._http_get(effective, path)
                self.breaker.record_success()
                return data
            except Exception as exc:  # noqa: BLE001 — 网络/HTTP 错误统一重试
                last_exc = exc
                ra = getattr(exc, "retry_after", None)
                retry_after = float(ra) if ra is not None else None
                self.breaker.record_failure()
            if attempt < self.MAX_RETRIES - 1:
                rng_wait = delay * random.uniform(0.7, 1.3)
                # Retry-After 与指数退避取大者：上游（blockstream 等）常给
                # 过小的默认值（1.0s），连续 429 时固定 1s 重试等于没有退避
                _time.sleep(max(retry_after if retry_after is not None else 0.0,
                                rng_wait))
                delay *= 2
        raise last_exc  # type: ignore[misc]

    def _http_get(self, base_url: str, path: str) -> object:
        import httpx

        resp = (self.httpx_client or httpx).get(
            f"{base_url}{path}", timeout=self.timeout_seconds)
        if resp.status_code == 429:
            ra = resp.headers.get("Retry-After")
            raise _SyncRateLimited(float(ra) if ra else 1.0)
        resp.raise_for_status()
        return resp.json()

    def _redis_get(self, key: str):
        if self.redis is None:
            return None
        try:
            raw = self.redis.get(key)
            return json.loads(raw) if raw is not None else None
        except Exception:  # noqa: BLE001 — 缓存层故障不阻塞主流程
            return None

    def _redis_set(self, key: str, data) -> None:
        if self.redis is None:
            return
        try:
            self.redis.set(key, json.dumps(data), ex=self.CACHE_TTL_SECONDS)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _map_tx(tx: dict) -> SimpleNamespace:
        return SimpleNamespace(
            txid=tx["txid"],
            inputs=[{
                "address": (vin.get("prevout") or {}).get("scriptpubkey_address"),
                "value": (vin.get("prevout") or {}).get("value", 0) / 1e8,
                # Esplora 每个 vin 直接携带其消费的 prevout 引用（spent_by 解析用）
                "prev_txid": vin.get("txid"),
                "prev_vout": vin.get("vout"),
            } for vin in tx.get("vin", [])],
            outputs=[{
                "address": vout.get("scriptpubkey_address"),
                "value": vout.get("value", 0) / 1e8,
                # issue #4：保留 OP_RETURN 脚本字段，供 CrosschainDetector 运行时检测
                "scriptpubkey": vout.get("scriptpubkey"),
                "scriptpubkey_asm": vout.get("scriptpubkey_asm"),
                "scriptpubkey_type": vout.get("scriptpubkey_type"),
                "scriptpubkey_address": vout.get("scriptpubkey_address"),
                # Liquid/Elements：vout.pegout 子对象单独保留（pegout 通道）
                "pegout": vout.get("pegout"),
            } for vout in tx.get("vout", [])],
            # Esplora 区块时间在 status.block_time；未确认交易无该字段 → None。
            # 兜底兼容部分 provider 在顶层携带 block_time 的形态。
            block_time=(tx.get("status") or {}).get("block_time") or tx.get("block_time"),
            # Esplora 响应自带每个 vout 的 spent 状态：未花输出即 unspent 终止依据
            unspent_outputs={
                f"{tx['txid']}:{i}" for i, v in enumerate(tx.get("vout", []))
                if not (v.get("status") or {}).get("spent", True)
            },
        )


class _SyncRateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"429 rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class BudgetExhaustedError(Exception):
    """issue #80：单次分析的建图请求/时长预算耗尽（builder 据此提前收敛）。"""
    error_code = "BUDGET_EXHAUSTED"


def build_provider(settings) -> tuple[object, list[str]]:
    """按 settings.graph_data_mode 返回 (provider, demo_seed_addresses)。"""
    if settings.graph_data_mode == "live":
        return LiveEsploraProvider(settings.esplora_api_url,
                                   redis_client=_sync_redis(),
                                   max_pages=settings.esplora_max_pages,
                                   request_budget=settings.graph_request_budget,
                                   time_budget_seconds=settings.graph_build_time_budget_seconds), []
    fixture = FixtureTxProvider.load()
    seeds = settings.demo_seeds_list or fixture.seed_addresses
    return fixture, seeds


def _sync_redis():
    """Redis 可达则复用为 Esplora L2 缓存；不可达是合法形态（无缓存）。"""
    try:
        from ..core.config import get_settings

        import redis

        client = redis.Redis.from_url(get_settings().redis_url,
                                      socket_connect_timeout=1)
        client.ping()
        return client
    except Exception:  # noqa: BLE001 — 与 orchestration._judgment_cache 同口径
        return None
