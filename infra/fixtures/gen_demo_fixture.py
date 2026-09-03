"""生成确定性演示子图夹具 infra/fixtures/demo_txs.json。

三个独立 seed 拓扑，对应阶段5 种子案例的三档风险：
- high   ：Lazarus playbook 全套（CoinJoin 入口 → peel 分层 → 宽扇出 → 跨链逃逸）
- low    ：普通钱包链（无混币器接触、浅层少量输出）
- normal ：单一钱包转账（最平凡结构）

地址由 sha256(name) 经真实 bech32m 编码产生——必须能通过 API 的
checksum 级校验（BE-11）。输出确定性：同脚本重跑字节级一致。

用法：python infra/fixtures/gen_demo_fixture.py
"""
from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.core.btc_address import encode_bech32m_address, validate_btc_address  # noqa: E402


def addr(name: str) -> str:
    return encode_bech32m_address(hashlib.sha256(f"pt-demo:{name}".encode()).digest()[:20])


BLOCK_TIME = 1700000000.0


def tx(txid: str, vin_addr: str, vin_value: float,
       outs: list[tuple[str, float]], *, unspent: bool = False) -> dict:
    return {
        "txid": txid,
        "inputs": [{"address": vin_addr, "value": vin_value}],
        "outputs": [{"address": a, "value": v} for a, v in outs],
        "block_time": BLOCK_TIME,
        **({"unspent_outputs": [0]} if unspent else {}),
    }


def op_return_output(data: bytes) -> dict:
    """构造带原始 OP_RETURN 脚本的输出（issue #4 运行时检测 fixture）。

    仅支持直接 pushdata 与 OP_PUSHDATA1/2/4，脚本首字节为 OP_RETURN(0x6a)。
    """
    n = len(data)
    if n < 0x4C:
        pre = bytes([0x6A, n])
        push = f"OP_PUSHDATA1 {n}"
    elif n <= 0xFF:
        pre = bytes([0x6A, 0x4C, n])
        push = f"OP_PUSHDATA1 {n}"
    elif n <= 0xFFFF:
        pre = bytes([0x6A, 0x4D]) + struct.pack("<H", n)
        push = f"OP_PUSHDATA2 {n}"
    else:
        pre = bytes([0x6A, 0x4E]) + struct.pack("<I", n)
        push = f"OP_PUSHDATA4 {n}"
    return {
        "address": None,
        "value": 0,
        "scriptpubkey": (pre + data).hex(),
        "scriptpubkey_asm": f"OP_RETURN {push}",
        "scriptpubkey_type": "op_return",
    }


def build_high(A: dict) -> tuple[list[dict], list[dict]]:
    """高风险拓扑；返回 (coinjoin_txids, txs_by_address 增量)。

    跨链逃逸交易用运行时 OP_RETURN 脚本表达（issue #4/#5），不预置 txid→protocol
    标签映射——跨链判定全部来自 CrosschainDetector。
    """
    cj_txid = "demo" + ("0" * 56) + "coinjoin"
    xb_txid = "demo" + ("1" * 56) + "xbridge"
    fund_txid = "demo" + ("7" * 60)
    txs = {
        # 种子为追踪目标：先接收外部注资（输出侧-only 根枚举需要「输出到该地址」的 UTXO），
        # 再向下游花销；注资交易无关联输入账本时用外部合成 prevout（第 4 个 args 的 `git` 说明见后）。
        A["seed"]: [
            # 注资入口（输出到种子，作为根 UTXO 来源）
            tx(fund_txid, A["fund_src"], 3.5, [(A["seed"], 2.0), (A["seed"], 1.5)]),
            # CoinJoin 入口：builder 命中 coinjoin 集合 → remixer 边终止
            tx(cj_txid, A["seed"], 2.0, [(A["peel1"], 0.5)]),
            tx("demo" + "2" * 60, A["seed"], 1.5,
               [(A["peel1"], 0.6), (A["change"], 0.85)]),
        ],
        A["peel1"]: [
            tx("demo" + "3" * 60, A["peel1"], 1.05,
               [(A["peel2a"], 0.25), (A["peel2b"], 0.72)]),
        ],
        A["peel2a"]: [
            # 跨链逃逸：运行时 OP_RETURN 检测命中 THORChain → 协议边终止展开
            {
                "txid": xb_txid,
                "inputs": [{"address": A["peel2a"], "value": 0.24}],
                "outputs": [op_return_output(b"SWAP:THOR.RUNE/ETH:0xdead:12"),
                            {"address": "bc1qxbridgeburn", "value": 0.23}],
                "block_time": BLOCK_TIME,
            },
        ],
        A["peel2b"]: [
            tx("demo" + "4" * 60, A["peel2b"], 0.70,
               [(A["peel3a"], 0.12), (A["peel3b"], 0.55)]),
        ],
        A["peel3a"]: [
            # unspent 沉淀分支：输出不展开（builder 计 unspent 终止单元）
            tx("demo" + "5" * 60, A["peel3a"], 0.11,
               [("bc1qsunkenoutput00000000000000000000000000000000000000", 0.10)],
               unspent=True),
        ],
        A["change"]: [
            tx("demo" + "6" * 60, A["change"], 0.83,
               [(A[f"fan{i}"], 0.09) for i in range(8)]),
        ],
    }
    return ([cj_txid], txs)


def build_low_and_normal(A: dict) -> dict:
    """低风险与普通钱包的浅层拓扑。"""
    return {
        A["low_seed"]: [
            # 注资入口（输出到低风险种子）
            tx("demo" + "8" * 60, A["fund_src"], 0.9, [(A["low_seed"], 0.9)]),
            tx("demo" + "a" * 60, A["low_seed"], 0.9,
               [(A["low_a"], 0.4), (A["low_b"], 0.45)]),
        ],
        A["low_a"]: [
            tx("demo" + "b" * 60, A["low_a"], 0.38,
               [("bc1qlowspend0000000000000000000000000000000000000000", 0.36)],
               unspent=True),
        ],
        A["normal_seed"]: [
            # 注资入口（输出到普通种子）
            tx("demo" + "9" * 60, A["fund_src"], 0.5, [(A["normal_seed"], 0.5)]),
            tx("demo" + "c" * 60, A["normal_seed"], 0.5,
               [(A["normal_out"], 0.48)]),
        ],
    }


def build_op_return_scenarios(A: dict) -> dict:
    """issue #4：带原始 OP_RETURN 脚本的检测场景交易样本（供 detection 单测直接加载）。

    独立于 txs_by_address（不参与演示子图构建），覆盖已支持协议 / unknown / malformed /
    多个 OP_RETURN / CoinJoin+Crosschain 同时命中；不依赖 CSV。
    """
    dest_a = A["peel2b"]
    dest_b = A["low_b"]

    def t(txid: str, inputs: list, outputs: list) -> dict:
        return {"txid": txid, "inputs": inputs, "outputs": outputs,
                "block_time": BLOCK_TIME}

    # 多个等额输入/输出（结构 CoinJoin 形状）→ 与 OP_RETURN 同时命中
    cj_inputs = [{"address": addr(f"cj_in_{j}"), "value": 0.1} for j in range(12)]
    cj_outputs = [{"address": addr(f"cj_out_{j}"), "value": 0.1} for j in range(12)]
    cj_outputs.append(op_return_output(b"SWAP:THOR.RUNE/ETH:0xdead:12"))

    return {
        "supported_thorchain": t(
            "demo" + "e" * 60,
            [{"address": A["peel1"], "value": 0.1}],
            [op_return_output(b"SWAP:THOR.RUNE/ETH:0xdead:12"),
             {"address": dest_a, "value": 0.09}],
        ),
        "unknown_op_return": t(
            "demo" + "f" * 60,
            [{"address": A["peel1"], "value": 0.1}],
            [op_return_output(b"HELLO UNKNOWN PROTOCOL"),
             {"address": dest_b, "value": 0.09}],
        ),
        "malformed_op_return": t(
            "demo" + "a1" * 30,
            [{"address": A["peel1"], "value": 0.1}],
            [{"address": None, "value": 0, "scriptpubkey": "6a",
              "scriptpubkey_asm": "OP_RETURN", "scriptpubkey_type": "op_return"},
             {"address": dest_a, "value": 0.09}],
        ),
        "multiple_op_return": t(
            "demo" + "b1" * 30,
            [{"address": A["peel1"], "value": 0.1}],
            [op_return_output(b"NOT A PROTOCOL"),
             op_return_output(b":SWAP:THOR.RUNE"),
             {"address": dest_a, "value": 0.09}],
        ),
        "coinjoin_plus_crosschain": t(
            "demo" + "c1" * 30, cj_inputs, cj_outputs),
    }


def main() -> int:
    names = {
        # high 拓扑
        "seed": "seed-high", "peel1": "peel1", "change": "change",
        "peel2a": "peel2a", "peel2b": "peel2b", "peel3a": "peel3a",
        "peel3b": "peel3b",
        **{f"fan{i}": f"fan{i}" for i in range(8)},
        # low / normal 拓扑
        "low_seed": "seed-low", "low_a": "low-a", "low_b": "low-b",
        "normal_seed": "seed-normal", "normal_out": "normal-out",
        # 各种子注资来源（外部地址，不参与向下展开）
        "fund_src": "lazarus-fund",
    }
    A = {k: addr(v) for k, v in names.items()}

    cj_ids, high_txs = build_high(A)
    scenarios = build_op_return_scenarios(A)
    data = {
        # 顺序即种子案例顺序：high / low / no_match 目标地址
        "seed_addresses": [A["seed"], A["low_seed"], A["normal_seed"]],
        "coinjoin_txids": cj_ids,
        "txs_by_address": {**high_txs, **build_low_and_normal(A)},
        "op_return_scenarios": scenarios,
    }

    bad = [a for a in (*data["seed_addresses"], *A.values())
           if validate_btc_address(a)]
    # 校验检测场景样本中非 OP_RETURN 输出地址
    for sc in scenarios.values():
        for out_ in sc["outputs"]:
            a = out_.get("address")
            if a and validate_btc_address(a):
                bad.append(a)
    if bad:
        raise SystemExit(f"generated invalid addresses: {bad}")

    out = ROOT / "infra" / "fixtures" / "demo_txs.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out} ({len(data['txs_by_address'])} addresses, "
          f"{len(data['seed_addresses'])} seeds)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
