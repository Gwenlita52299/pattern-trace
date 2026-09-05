"""BTC 地址校验（backend-api-spec §3 analyze Validation）。

checksum 级而非正则：bech32/bech32m 走 BIP173/BIP350 polymod，并完整校验
witness 版本与 program 长度规则（issue #24）；
P2PKH/P2SH 走 base58check 的 sha256d 校验和 + 主网 version byte。
"""
from __future__ import annotations

import hashlib

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32M_CONST = 0x2BC830A3
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# BIP141 witness 规则（issue #24）
_WITNESS_VERSION_MAX = 16
_WITNESS_PROGRAM_MIN = 2
_WITNESS_PROGRAM_MAX = 40
# BIP173：bech32 字符串总长上限 90（含 v1 40 字节程序的合法上限 74）
_BECH32_MAX_LEN = 90
# 主网 Base58Check：P2PKH (0x00) / P2SH (0x05)，payload 21 字节 + checksum 4
_BASE58_VERSIONS = {0x00, 0x05}
_BASE58_TOTAL_LEN = 25


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


def _decode_witness_address(address: str) -> tuple[int, bytes] | str:
    """完整解析 bech32/bech32m 地址为 (witness_version, program)。

    依据 BIP173/BIP350：校验大小写、charset、长度、polymod，再按 witness
    规则校验 version（0..16）、program 长度（2..40；v0 仅 20/32）以及
    编码类型与版本的对应（v0→bech32，v1+→bech32m）。失败返回错误信息。
    """
    if address != address.lower() and address != address.upper():
        return "mixed-case bech32 violates BIP173"
    addr = address.lower()
    pos = addr.rfind("1")
    # separator 后至少 6（checksum）+1（version）个字符
    if pos < 1 or pos + 7 > len(addr) or len(addr) > _BECH32_MAX_LEN:
        return "invalid bech32 structure"
    hrp, data_part = addr[:pos], addr[pos + 1:]
    if hrp != "bc":
        return f"unexpected HRP {hrp!r} (mainnet 'bc' only)"
    try:
        values = [_BECH32_CHARSET.index(c) for c in data_part]
    except ValueError:
        return "invalid bech32 character"
    polymod = _bech32_polymod(_bech32_hrp_expand(hrp) + values)
    if polymod == 1:
        encoding = "bech32"
    elif polymod == _BECH32M_CONST:
        encoding = "bech32m"
    else:
        return ("invalid bech32 checksum (BIP173/BIP350 polymod mismatch): "
                f"{address}")

    version = values[0]
    # data_part = version(1) + program(N) + checksum(6)
    program = _convertbits_strict(values[1:-6], 5, 8)
    if program is None:
        return "witness program padding bits must be zero"
    if version > _WITNESS_VERSION_MAX:
        return f"witness version {version} out of range 0..16"
    if version == 0:
        if encoding != "bech32":
            return "witness version 0 must use bech32 encoding (BIP350)"
        if len(program) not in (20, 32):
            return ("witness version 0 program must be 20 or 32 bytes, "
                    f"got {len(program)}")
    else:
        if encoding != "bech32m":
            return (f"witness version {version} must use bech32m encoding "
                    "(BIP350)")
        if not (_WITNESS_PROGRAM_MIN <= len(program)
                <= _WITNESS_PROGRAM_MAX):
            return (f"witness program length {len(program)} out of "
                    f"range {_WITNESS_PROGRAM_MIN}..{_WITNESS_PROGRAM_MAX} bytes")
    return version, program


def _verify_base58check(address: str) -> str | None:
    """校验 base58check 及主网 P2PKH/P2SH payload；失败返回错误信息。"""
    num = 0
    for ch in address:
        idx = _BASE58_ALPHABET.find(ch)
        if idx < 0:
            return "invalid base58 character"
        num = num * 58 + idx
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big")
    # 前导 '1' 每个代表一个零字节
    raw = b"\x00" * (len(address) - len(address.lstrip("1"))) + raw
    # issue #24：主网 P2PKH/P2SH 固定 25 字节（version 1 + hash160 20 + checksum 4）
    if len(raw) != _BASE58_TOTAL_LEN:
        return (f"invalid base58check payload length {len(raw)} "
                f"(mainnet P2PKH/P2SH must be {_BASE58_TOTAL_LEN})")
    payload, checksum = raw[:-4], raw[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if checksum != expected:
        return f"invalid base58check checksum: {address}"
    version = payload[0]
    if version not in _BASE58_VERSIONS:
        return (f"unsupported base58 version byte 0x{version:02x} "
                "(mainnet P2PKH 0x00 / P2SH 0x05 only)")
    return None


def validate_btc_address(address: str) -> str | None:
    """合法返回 None；否则返回可展示的拒绝原因（供 422 detail 使用）。"""
    if not isinstance(address, str) or not address:
        return "address must be a non-empty string"
    if len(address) > _BECH32_MAX_LEN:
        return f"address exceeds {_BECH32_MAX_LEN} characters"
    if address.startswith(("bc1", "BC1")):
        decoded = _decode_witness_address(address)
        return decoded if isinstance(decoded, str) else None
    if address[0] in ("1", "3"):
        return _verify_base58check(address)
    return f"unrecognized Bitcoin address format: {address}"


# ---------------------------------------------------------------------------
# 编码端（仅演示夹具生成器使用）：为给定 witness 程序计算合法 bc 地址
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


def _convertbits_strict(data: list[int], frombits: int,
                        tobits: int) -> list[int] | None:
    """无填充重分组：剩余位数超限或非零填充位均为非法（BIP173 witness）。

    参考实现（sipa/bech32）：pad=False 时若 bits >= frombits 或最终非零
    填充位，则重分组非法。
    """
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def encode_bech32m_address(witness_program: bytes, witness_version: int = 0) -> str:
    """构造合法测试用 bc 地址（issue #24：v0→bech32，v1+→bech32m）。"""
    data = [witness_version] + _convertbits(list(witness_program), 8, 5)
    values = _bech32_hrp_expand("bc") + data
    if witness_version == 0:
        polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    else:
        polymod = (_bech32_polymod(values + [0, 0, 0, 0, 0, 0])
                   ^ _BECH32M_CONST)
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return "bc1" + "".join(_BECH32_CHARSET[d] for d in data + checksum)
