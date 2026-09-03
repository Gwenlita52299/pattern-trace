"""backend.detection 跨链检测单测 — issue #4 模块化运行时 OP_RETURN 检测。

覆盖：
- OpReturnParser：pushdata1/2/4、直接 pushdata、多个 OP_RETURN vout/payload、
  空 payload / 非法 hex / 截断脚本 → malformed。
- 协议 Decoder：thorchain（SWAP:/:SWAP:/s:）、liquid pegout；独立 Decoder 与测试。
- CrosschainDetector：已支持协议确认跨链；unknown/malformed 不直接标记；
  Decoder 冲突 → ambiguous（不停止扩展）。
- GraphBuilder 接线：命中 OP_RETURN → early_stop_crosschain + op_return_protocol，
  且只依赖 Detector（无 crosschain_tx_set 也能命中）；ambiguous 不停止。
- Esplora live provider 保留 OP_RETURN / pegout 脚本字段。

全部为合成数据，不依赖 CSV / 外部数据 / DB。
"""
import struct
from dataclasses import dataclass, field

from backend.detection import (
    CrosschainDetector,
    CrosschainType,
    OpReturnParser,
)
from backend.detection.models import OpReturnPayload, PegoutEvidence
from backend.detection.protocols import PegoutDecoder, ThorchainDecoder
from backend.detection.protocols.registry import DecoderRegistry
from backend.graph_builder.builder import GraphBuilder
from backend.graph_builder.data_source import LiveEsploraProvider

# ---------------------------------------------------------------------------
# 合成 OP_RETURN 脚本工具
# ---------------------------------------------------------------------------
def push_script(data: bytes) -> bytes:
    """构造 OP_RETURN + pushdata 脚本字节（首字节 0x6a）。"""
    n = len(data)
    out = bytearray([0x6A])  # OP_RETURN
    if n < 0x4C:
        out.append(n)
    elif n <= 0xFF:
        out += bytes([0x4C, n])  # OP_PUSHDATA1
    elif n <= 0xFFFF:
        out += bytes([0x4D]) + struct.pack("<H", n)  # OP_PUSHDATA2
    else:
        out += bytes([0x4E]) + struct.pack("<I", n)  # OP_PUSHDATA4
    out += data
    return bytes(out)


def op_return(data: bytes, index: int | None = None) -> dict:
    script = push_script(data)
    return {
        "address": None,
        "value": 0,
        "scriptpubkey": script.hex(),
        "scriptpubkey_asm": f"OP_RETURN OP_PUSHDATA1 {len(data)}",
        "scriptpubkey_type": "op_return",
    }


def make_tx(outputs: list[dict], txid: str = "xtx") -> dict:
    return {"txid": txid, "outputs": outputs}


# ---------------------------------------------------------------------------
# OpReturnParser
# ---------------------------------------------------------------------------
class TestOpReturnParser:
    def test_parse_pushdata1(self):
        data = b"A" * 100
        script = bytes([0x6A, 0x4C, len(data)]) + data  # OP_RETURN OP_PUSHDATA1
        p = OpReturnParser().parse_vout(
            {"address": None, "value": 0, "scriptpubkey": script.hex(),
             "scriptpubkey_asm": f"OP_RETURN OP_PUSHDATA1 {len(data)}",
             "scriptpubkey_type": "op_return"}, 0)[0]
        assert p.valid and p.payload == data

    def test_parse_pushdata2(self):
        data = b"A" * 300
        script = bytes([0x6A, 0x4D]) + struct.pack("<H", len(data)) + data
        p = OpReturnParser().parse_vout(
            {"address": None, "value": 0, "scriptpubkey": script.hex(),
             "scriptpubkey_asm": "OP_RETURN OP_PUSHDATA2", "scriptpubkey_type": "op_return"},
            0)[0]
        assert p.valid and p.payload == data

    def test_parse_pushdata4(self):
        data = b"B" * 70000
        script = bytes([0x6A, 0x4E]) + struct.pack("<I", len(data)) + data  # OP_PUSHDATA4
        p = OpReturnParser().parse_vout(
            {"address": None, "value": 0, "scriptpubkey": script.hex(),
             "scriptpubkey_asm": "OP_RETURN OP_PUSHDATA4", "scriptpubkey_type": "op_return"},
            0)[0]
        assert p.valid and p.payload == data

    def test_truncated_direct_push(self):
        # 直接 pushdata 声明长度 5，但只给 2 字节 → 截断
        script = bytes([0x6A, 0x05, 0x01, 0x02])
        p = OpReturnParser().parse_vout(
            {"address": None, "value": 0, "scriptpubkey": script.hex(),
             "scriptpubkey_asm": "OP_RETURN", "scriptpubkey_type": "op_return"}, 0)[0]
        assert p.valid is False and p.error == "truncated"

    def test_truncated_pushdata1_no_length_byte(self):
        # OP_PUSHDATA1 (0x4c) 直接结尾，无长度字节 → 截断
        script = bytes([0x6A, 0x4C])
        p = OpReturnParser().parse_vout(
            {"address": None, "value": 0, "scriptpubkey": script.hex(),
             "scriptpubkey_asm": "OP_RETURN", "scriptpubkey_type": "op_return"}, 0)[0]
        assert p.valid is False and p.error == "truncated"

    def test_truncated_pushdata1_len_exceeds(self):
        # OP_PUSHDATA1 声明 5 字节但只给 2 字节 → 截断
        script = bytes([0x6A, 0x4C, 0x05, 0x01, 0x02])
        p = OpReturnParser().parse_vout(
            {"address": None, "value": 0, "scriptpubkey": script.hex(),
             "scriptpubkey_asm": "OP_RETURN", "scriptpubkey_type": "op_return"}, 0)[0]
        assert p.valid is False and p.error == "truncated"

    def test_skip_non_push_opcode(self):
        # OP_RETURN OP_1(0x51) OP_DROP(0x75) 0x01 0x41 → 只提取 1 字节 pushdata
        script = bytes([0x6A, 0x51, 0x75, 0x01, 0x41])
        ps = OpReturnParser().parse_vout(
            {"address": None, "value": 0, "scriptpubkey": script.hex(),
             "scriptpubkey_asm": "OP_RETURN OP_1 OP_DROP", "scriptpubkey_type": "op_return"}, 0)
        assert [p.payload for p in ps] == [b"A"]

    def test_op_return_asm_without_type(self):
        # 无 scriptpubkey_type，仅凭 asm 以 OP_RETURN 开头判定
        script = push_script(b"SWAP:THOR.RUNE")
        ps = OpReturnParser().parse_vout(
            {"address": None, "value": 0, "scriptpubkey": script.hex(),
             "scriptpubkey_asm": "OP_RETURN OP_PUSHDATA1 5"}, 0)
        assert len(ps) == 1 and ps[0].valid

    def test_raw_first_byte_only(self):
        # 无 type 无 asm，仅凭 raw 脚本首字节 0x6a 判定为 OP_RETURN
        script = push_script(b"HI")
        ps = OpReturnParser().parse_vout(
            {"address": None, "value": 0, "scriptpubkey": script.hex()}, 0)
        assert len(ps) == 1 and ps[0].valid

    def test_invalid_hex_non_op_return_empty(self):
        # 非 OP_RETURN 类型 + 非法 hex → 不产生 payload（空列表）
        ps = OpReturnParser().parse_vout(
            {"address": "bc1qfoo", "value": 0.2, "scriptpubkey": "zzzz",
             "scriptpubkey_asm": "OP_0", "scriptpubkey_type": "v0_p2wpkh"}, 0)
        assert ps == []

    def test_parse_vout_simplespace(self):
        # vout 为 SimpleNamespace（live provider 可能形态）同样可解析
        from types import SimpleNamespace

        script = push_script(b"s:THOR.RUNE")
        vout = SimpleNamespace(address=None, value=0, scriptpubkey=script.hex(),
                               scriptpubkey_asm="OP_RETURN", scriptpubkey_type="op_return")
        ps = OpReturnParser().parse_vout(vout, 0)
        assert len(ps) == 1 and ps[0].valid

    def test_direct_pushdata(self):
        # 直接 pushdata (0x01..0x4b)
        data = b"0123456789"
        script = bytes([0x6A, len(data)]) + data
        p = OpReturnParser().parse_vout(
            {"address": None, "value": 0, "scriptpubkey": script.hex(),
             "scriptpubkey_asm": "OP_RETURN", "scriptpubkey_type": "op_return"}, 0)[0]
        assert p.valid and p.payload == data

    def test_multiple_payloads_in_one_script(self):
        script = bytes([0x6A, 0x01, 0x41, 0x01, 0x42])  # OP_RETURN 1 0x41 1 0x42
        ps = OpReturnParser().parse_vout(
            {"address": None, "value": 0, "scriptpubkey": script.hex(),
             "scriptpubkey_asm": "OP_RETURN", "scriptpubkey_type": "op_return"}, 0)
        assert [p.payload for p in ps] == [b"A", b"B"]

    def test_multiple_op_return_vouts(self):
        tx = make_tx([op_return(b"SWAP:THOR.RUNE"), op_return(b"HELLO"),
                      {"address": "bc1qfoo", "value": 0.2}])
        ps = OpReturnParser().parse_tx(tx)
        assert len(ps) == 2
        assert [p.vout for p in ps] == [0, 1]

    def test_empty_payload_malformed(self):
        tx = make_tx([{"address": None, "value": 0, "scriptpubkey": "6a",
                       "scriptpubkey_asm": "OP_RETURN", "scriptpubkey_type": "op_return"}])
        p = OpReturnParser().parse_tx(tx)[0]
        assert p.valid is False and p.error == "empty_payload"

    def test_invalid_hex_malformed(self):
        tx = make_tx([{"address": None, "value": 0, "scriptpubkey": "zzzz",
                       "scriptpubkey_asm": "OP_RETURN", "scriptpubkey_type": "op_return"}])
        p = OpReturnParser().parse_tx(tx)[0]
        assert p.valid is False and p.error == "invalid_hex"

    def test_truncated_malformed(self):
        # PUSHDATA1 声明 len=5 但只给 2 字节 → 截断
        script = bytes([0x6A, 0x4C, 0x05, 0x01, 0x02])
        p = OpReturnParser().parse_vout(
            {"address": None, "value": 0, "scriptpubkey": script.hex(),
             "scriptpubkey_asm": "OP_RETURN", "scriptpubkey_type": "op_return"}, 0)[0]
        assert p.valid is False and p.error == "truncated"

    def test_ignores_non_op_return_output(self):
        tx = make_tx([{"address": "bc1qfoo", "value": 0.2, "scriptpubkey": "0014" + "00" * 20,
                       "scriptpubkey_asm": "OP_0 OP_PUSHBYTES_20",
                       "scriptpubkey_type": "v0_p2wpkh"}])
        assert OpReturnParser().parse_tx(tx) == []


# ---------------------------------------------------------------------------
# 协议 Decoder
# ---------------------------------------------------------------------------
class TestThorchainDecoder:
    def test_classic_swap(self):
        d = ThorchainDecoder()
        m = d.try_decode(OpReturnPayload(vout=0, payload=b"SWAP:THOR.RUNE/ETH:0xdead:12"))
        assert m is not None and m.protocol == "thorchain" and m.channel == "op_return"
        assert m.decoder_version

    def test_colon_prefix(self):
        d = ThorchainDecoder()
        assert d.try_decode(OpReturnPayload(vout=0, payload=b":SWAP:THOR.RUNE")) is not None
        assert d.try_decode(OpReturnPayload(vout=0, payload=b":s:THOR.RUNE")) is not None

    def test_compact_prefix(self):
        d = ThorchainDecoder()
        assert d.try_decode(OpReturnPayload(vout=0, payload=b"s:THOR.RUNE")) is not None

    def test_rejects_unknown(self):
        d = ThorchainDecoder()
        assert d.try_decode(OpReturnPayload(vout=0, payload=b"HELLO WORLD")) is None

    def test_rejects_invalid_channel(self):
        d = ThorchainDecoder()
        assert d.try_decode(PegoutEvidence(vout=0, pegout_scriptpubkey_asm="OP_0")) is None


class TestPegoutDecoder:
    def test_matches_pegout(self):
        d = PegoutDecoder()
        m = d.try_decode(PegoutEvidence(vout=1, pegout_scriptpubkey="0014" + "00" * 20,
                                        pegout_scriptpubkey_asm="OP_0 OP_PUSHBYTES_20"))
        assert m is not None and m.protocol == "liquid_pegout" and m.channel == "pegout"

    def test_rejects_empty(self):
        d = PegoutDecoder()
        assert d.try_decode(PegoutEvidence(vout=0)) is None

    def test_rejects_op_return_channel(self):
        d = PegoutDecoder()
        assert d.try_decode(OpReturnPayload(vout=0, payload=b"SWAP:THOR.RUNE")) is None


# ---------------------------------------------------------------------------
# CrosschainDetector
# ---------------------------------------------------------------------------
class TestCrosschainDetector:
    def test_supported_protocol_confirms(self):
        d = CrosschainDetector().detect(make_tx([op_return(b"SWAP:THOR.RUNE/ETH:0xdead")]))
        assert d.is_crosschain is True
        assert d.protocol == "thorchain"
        assert d.type == CrosschainType.OP_RETURN
        assert d.reason == "op_return_protocol"
        assert d.parser_version and d.detector_version and d.decoder_version

    def test_unknown_op_return_not_crosschain(self):
        d = CrosschainDetector().detect(make_tx([op_return(b"NOTA PROTOCOL")]))
        assert d.is_crosschain is False
        assert d.reason == "unknown_op_return"

    def test_malformed_op_return_not_crosschain(self):
        d = CrosschainDetector().detect(make_tx([
            {"address": None, "value": 0, "scriptpubkey": "6a",
             "scriptpubkey_asm": "OP_RETURN", "scriptpubkey_type": "op_return"}]))
        assert d.is_crosschain is False
        assert d.reason == "malformed_op_return"

    def test_multiple_op_return_one_known(self):
        tx = make_tx([op_return(b"THORCHAIN? no"), op_return(b"SWAP:THOR.RUNE")])
        d = CrosschainDetector().detect(tx)
        assert d.is_crosschain is True and d.protocol == "thorchain"

    def test_ambiguous_conflict_not_crosschain(self):
        # 同一笔交易同时命中 OP_RETURN(thorchain) 与 pegout(liquid_pegout) → 不同协议 → ambiguous
        tx = make_tx([op_return(b"SWAP:THOR.RUNE"), {
            "address": None, "value": 0.5, "scriptpubkey": "6a", "scriptpubkey_type": "pegout",
            "pegout": {"scriptpubkey": "0014" + "00" * 20, "scriptpubkey_asm": "OP_0",
                       "scriptpubkey_type": "v0_p2wpkh", "value": 0.5},
        }])
        d = CrosschainDetector().detect(tx)
        assert d.is_crosschain is False
        assert d.type == CrosschainType.AMBIGUOUS
        assert d.reason == "ambiguous"

    def test_no_op_return_not_crosschain(self):
        d = CrosschainDetector().detect(make_tx([{"address": "bc1qfoo", "value": 0.2}]))
        assert d.is_crosschain is False and d.reason == "none"

    def test_pegout_channel_confirms(self):
        tx = make_tx([{"address": None, "value": 0.5, "scriptpubkey": "6a",
                       "scriptpubkey_type": "pegout",
                       "pegout": {"scriptpubkey": "0014" + "00" * 20,
                                  "scriptpubkey_asm": "OP_0 OP_PUSHBYTES_20",
                                  "scriptpubkey_type": "v0_p2wpkh", "value": 0.5}}])
        d = CrosschainDetector().detect(tx)
        assert d.is_crosschain is True and d.protocol == "liquid_pegout"
        assert d.type == CrosschainType.PEGOUT

    def test_custom_registry_without_pegout(self):
        reg = DecoderRegistry()
        reg.register_op_return(ThorchainDecoder())  # 无 pegout decoder
        # pegout 证据在无 pegout decoder 时不被识别
        tx = make_tx([{"address": None, "value": 0.5, "scriptpubkey": "6a",
                       "scriptpubkey_type": "pegout",
                       "pegout": {"scriptpubkey": "0014" + "00" * 20,
                                  "scriptpubkey_asm": "OP_0",
                                  "scriptpubkey_type": "v0_p2wpkh", "value": 0.5}}])
        d = CrosschainDetector(registry=reg).detect(tx)
        assert d.is_crosschain is False and d.reason == "none"


# ---------------------------------------------------------------------------
# GraphBuilder 接线（只依赖 Detector）
# ---------------------------------------------------------------------------
SEED = "bc1qseed000000000000000000000000000000000000000000000000qa"
V = 0.001


@dataclass
class MockTx:
    txid: str
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    block_time: float = 1700000000.0


def vin(addr: str, value: float, prev_txid: str, prev_vout: int = 0) -> dict:
    return {"address": addr, "value": value, "prev_txid": prev_txid, "prev_vout": prev_vout}


def make_provider(tx_map):
    """callable 桩：address -> tx 列表（含自动补注资，输出侧-only 根枚举）。"""
    from collections import defaultdict

    builder: dict[str, list] = defaultdict(list)
    for addr, txs in tx_map.items():
        builder[addr].extend(txs)
    existing = {getattr(t, "txid", None) for txs in builder.values() for t in txs}

    pending: dict[str, dict[int, tuple[str, float]]] = defaultdict(dict)
    for addr, txs in tx_map.items():
        for tx in txs:
            for inp in getattr(tx, "inputs", None) or []:
                ptxid = inp.get("prev_txid")
                if not ptxid or ptxid in existing:
                    continue
                pending[ptxid].setdefault(inp.get("prev_vout", 0),
                                          (inp.get("address"), inp.get("value", V)))

    for ptxid, outs in pending.items():
        maxidx = max(outs)
        outputs = [{"address": None, "value": V} for _ in range(maxidx + 1)]
        for idx, (owner, value) in outs.items():
            outputs[idx] = {"address": owner, "value": value}
        for owner, _v in outs.values():
            builder[owner].append(MockTx(txid=ptxid, outputs=outputs))
    return lambda addr: builder.get(addr, [])


class TestBuilderWiring:
    def test_runtime_op_return_triggers_early_stop_without_set(self):
        spender = MockTx(
            txid="bridge_tx",
            inputs=[vin(SEED, 0.5, "u1")],
            outputs=[op_return(b"SWAP:THOR.RUNE/ETH:0xdead:12"),
                     {"address": "bc1qburn", "value": 0.23}],
        )
        builder = GraphBuilder()  # 无 crosschain_tx_set → 依赖运行时 Detector
        result = builder.build(SEED, make_provider({SEED: [spender]}), hops=1)
        assert result.stats.early_stop_crosschain == 1
        cross = [e for e in result.edges if e.is_crosschain]
        assert len(cross) == 1
        assert cross[0].op_return_protocol == "thorchain"
        assert cross[0].is_stopped_expansion is True

    def test_ambiguous_does_not_stop_expansion(self):
        # OP_RETURN thorchain + pegout → ambiguous → 不停止（视为 expanded）
        spender = MockTx(
            txid="both_channel",
            inputs=[vin(SEED, 0.5, "u1")],
            outputs=[op_return(b"SWAP:THOR.RUNE"),
                     {"address": None, "value": 0.5, "scriptpubkey": "6a",
                      "scriptpubkey_type": "pegout",
                      "pegout": {"scriptpubkey": "0014" + "00" * 20,
                                 "scriptpubkey_asm": "OP_0", "value": 0.5}},
                     {"address": "bc1qout", "value": 0.1}],
        )
        builder = GraphBuilder()
        result = builder.build(SEED, make_provider({SEED: [spender]}), hops=1)
        assert result.stats.early_stop_crosschain == 0
        assert not any(e.is_crosschain for e in result.edges)
        # ambiguous → 至少被当作 expanded 展开
        assert result.stats.expanded >= 1 or result.stats.tx4_new_dst_hard_stop >= 1

    def test_coinjoin_priority_over_crosschain(self):
        # 结构 CoinJoin + OP_RETURN thorchain 同时命中 → CoinJoin 优先（early_stop_wasabi）。
        # 种子只拥有 u0 这一个 UTXO（其余输入归其他地址），故只处理 1 个单元。
        inputs = [vin(SEED, 0.1, "u0")] + [
            vin(f"bc1q_in_{j}", 0.1, f"u{j}") for j in range(1, 12)]
        outputs = [{"address": f"bc1q_out_{j}", "value": 0.1} for j in range(12)]
        outputs.append(op_return(b"SWAP:THOR.RUNE"))
        spender = MockTx(txid="both_mixed", inputs=inputs, outputs=outputs)
        builder = GraphBuilder()
        result = builder.build(SEED, make_provider({SEED: [spender]}), hops=1)
        # CoinJoin（等额输出 + 扇入/扇出宽度）先命中 → 不按 crosschain 终止
        assert result.stats.early_stop_wasabi == 1
        assert result.stats.early_stop_crosschain == 0
        assert any(e.is_remixer for e in result.edges)
        assert not any(e.is_crosschain for e in result.edges)

    def test_crosschain_tx_set_still_fallback(self):
        # 无 OP_RETURN 字段但 txid 在标签集 → 标签库兜底仍生效（向后兼容）
        spender = MockTx(txid="known_lbl", inputs=[vin(SEED, 0.5, "u1")],
                         outputs=[{"address": "bc1qburn", "value": 0.23}])
        builder = GraphBuilder(crosschain_tx_set={"known_lbl": "runes"})
        result = builder.build(SEED, make_provider({SEED: [spender]}), hops=1)
        assert result.stats.early_stop_crosschain == 1
        assert [e for e in result.edges if e.is_crosschain][0].op_return_protocol == "runes"


# ---------------------------------------------------------------------------
# Esplora live provider 保留脚本字段
# ---------------------------------------------------------------------------
class TestLiveProviderPreservesScriptFields:
    def test_map_tx_preserves_op_return_and_pegout(self):
        raw_tx = {
            "txid": "a" * 64,
            "vin": [{"txid": "b" * 64, "vout": 0,
                     "prevout": {"scriptpubkey_address": "bc1qin", "value": 100000000}}],
            "vout": [
                {"scriptpubkey": "6a1c" + b"SWAP:THOR".hex(),
                 "scriptpubkey_asm": "OP_RETURN OP_PUSHDATA1 5",
                 "scriptpubkey_type": "op_return", "scriptpubkey_address": None,
                 "value": 0},
                {"scriptpubkey": "0014" + "00" * 20,
                 "scriptpubkey_asm": "OP_0 OP_PUSHBYTES_20",
                 "scriptpubkey_type": "v0_p2wpkh", "scriptpubkey_address": "bc1qout",
                 "value": 50000000,
                 "pegout": {"scriptpubkey": "0014" + "11" * 20,
                            "scriptpubkey_asm": "OP_0 OP_PUSHBYTES_20",
                            "scriptpubkey_type": "v0_p2wpkh", "value": 20000000}},
            ],
            "status": {"block_time": 1700000000, "spent": True},
        }
        mapped = LiveEsploraProvider._map_tx(raw_tx)
        outs = mapped.outputs
        assert outs[0]["scriptpubkey_type"] == "op_return"
        assert outs[0]["scriptpubkey"].startswith("6a")
        assert outs[0]["address"] is None
        # pegout 子对象完整保留
        assert outs[1]["pegout"]["scriptpubkey_type"] == "v0_p2wpkh"
        # 检测结果：该交易含 OP_RETURN，但此处无协议 match 字段供 detector 用
        assert outs[1]["value"] == 0.5


# ---------------------------------------------------------------------------
# Fixture 端到端：运行时检测命中 crosschain（不依赖 crosschain_tx_set）
# ---------------------------------------------------------------------------
class TestFixtureEndToEnd:
    def test_fixture_crosschain_tx_detected_at_runtime(self):
        from backend.graph_builder.data_source import FixtureTxProvider

        fp = FixtureTxProvider.load()
        xb = "demo11111111111111111111111111111111111111111111111111111111xbridge"
        tx = fp.get_tx(xb)
        assert tx is not None
        d = CrosschainDetector().detect(tx)
        assert d.is_crosschain is True and d.protocol == "thorchain"

        # 从主种子建图（hops=3 到达 peel 链末端的跨链交易）→ 运行时命中 crosschain
        seed = fp.seed_addresses[0]
        result = GraphBuilder().build(
            seed, fp, hops=3, time_window_days=90, seed_block_time=fp.seed_block_time(seed))
        assert result.stats.early_stop_crosschain == 1
        cross = [e for e in result.edges if e.is_crosschain]
        assert len(cross) == 1
        assert cross[0].op_return_protocol == "thorchain"

    def test_fixture_op_return_scenarios(self):
        """op_return_scenarios 覆盖已支持/unknown/malformed/多 OP_RETURN/CoinJoin 重叠。"""
        from backend.graph_builder.data_source import FixtureTxProvider

        fp = FixtureTxProvider.load()
        sc = fp.op_return_scenarios
        assert sc, "fixture 应提供 op_return_scenarios"

        det = CrosschainDetector()
        # 已支持协议 → 确认跨链
        d = det.detect(sc["supported_thorchain"])
        assert d.is_crosschain is True and d.protocol == "thorchain"
        # unknown OP_RETURN → 不直接标记跨链
        d = det.detect(sc["unknown_op_return"])
        assert d.is_crosschain is False and d.reason == "unknown_op_return"
        # malformed OP_RETURN → 不直接标记跨链
        d = det.detect(sc["malformed_op_return"])
        assert d.is_crosschain is False and d.reason == "malformed_op_return"
        # 多个 OP_RETURN 中有一个已支持 → 确认跨链
        d = det.detect(sc["multiple_op_return"])
        assert d.is_crosschain is True and d.protocol == "thorchain"
        # CoinJoin 结构 + OP_RETURN 同时命中：detector 级确认跨链（builder 级优先级另测）
        d = det.detect(sc["coinjoin_plus_crosschain"])
        assert d.is_crosschain is True and d.protocol == "thorchain"
