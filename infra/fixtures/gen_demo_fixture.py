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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.core.btc_address import encode_bech32m_address, validate_btc_address


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


def build_high(A: dict) -> tuple[list[dict], list[dict], list]:
    """高风险拓扑；返回 (coinjoin_txids, crosschain 条目, txs_by_address 增量)。"""
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
            # 跨链逃逸：命中 crosschain 集 → 协议边终止展开
            tx(xb_txid, A["peel2a"], 0.24, [("bc1qxbridgeburn", 0.23)]),
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
    return ([cj_txid],
            [{"txid": xb_txid, "protocol": "thorchain_swap"}],
            txs)


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

    cj_ids, xc_entries, high_txs = build_high(A)
    data = {
        # 顺序即种子案例顺序：high / low / no_match 目标地址
        "seed_addresses": [A["seed"], A["low_seed"], A["normal_seed"]],
        "coinjoin_txids": cj_ids,
        "crosschain_tx_set": {e["txid"]: e["protocol"] for e in xc_entries},
        "txs_by_address": {**high_txs, **build_low_and_normal(A)},
    }

    bad = [a for a in (*data["seed_addresses"], *A.values())
           if validate_btc_address(a)]
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
