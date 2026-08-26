"""BTC 地址校验（backend-api-spec §3 analyze Validation）。

checksum 级而非正则：bech32/bech32m 走 BIP173/BIP350 polymod；
P2PKH/P2SH 走 base58check 的 sha256d 校验和。
"""
from __future__ import annotations

import hashlib

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32M_CONST = 0x2BC830A3
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _bech32_polymod(values: list[int]) -> int:
    generators = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            chk ^= generators[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _verify_bech32_checksum(address: str) -> bool:
    """返回 True 当且仅当 bech32 或 bech32m 校验和之一成立。"""
    if address != address.lower() and address != address.upper():
        return False  # 混合大小写违反 BIP173
    addr = address.lower()
    pos = addr.rfind("1")
    if pos < 1 or pos + 7 > len(addr) or len(addr) > 90:
        return False
    hrp, data_part = addr[:pos], addr[pos + 1:]
    if hrp != "bc":
        return False
    try:
        values = [_BECH32_CHARSET.index(c) for c in data_part]
    except ValueError:
        return False
    polymod = _bech32_polymod(_bech32_hrp_expand(hrp) + values)
    return polymod in (1, _BECH32M_CONST)


def _verify_base58check(address: str) -> bool:
    num = 0
    for ch in address:
        idx = _BASE58_ALPHABET.find(ch)
        if idx < 0:
            return False
        num = num * 58 + idx
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big")
    # 前导 '1' 每个代表一个零字节
    raw = b"\x00" * (len(address) - len(address.lstrip("1"))) + raw
    if len(raw) < 5:
        return False
    payload, checksum = raw[:-4], raw[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return checksum == expected


def validate_btc_address(address: str) -> str | None:
    """合法返回 None；否则返回可展示的拒绝原因（供 422 detail 使用）。"""
    if not isinstance(address, str) or not address:
        return "address must be a non-empty string"
    if len(address) > 62:
        return "address exceeds 62 characters"
    if address.startswith(("bc1", "BC1")):
        if not _verify_bech32_checksum(address):
            return ("invalid bech32 checksum (BIP173/BIP350 polymod mismatch): "
                    f"{address}")
        return None
    if address[0] in ("1", "3"):
        if not _verify_base58check(address):
            return f"invalid base58check checksum: {address}"
        return None
    return f"unrecognized Bitcoin address format: {address}"


# ---------------------------------------------------------------------------
# 编码端（仅演示夹具生成器使用）：为给定 witness 程序计算合法 bech32m 地址
# ---------------------------------------------------------------------------
def _convertbits(data: list[int], frombits: int, tobits: int, pad: bool = True) -> list[int]:
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def encode_bech32m_address(witness_program: bytes, witness_version: int = 0) -> str:
    """构造通过 BIP350 polymod 的测试用 bc 地址。"""
    data = [witness_version] + _convertbits(list(witness_program), 8, 5)
    values = _bech32_hrp_expand("bc") + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ _BECH32M_CONST
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return "bc1" + "".join(_BECH32_CHARSET[d] for d in data + checksum)
