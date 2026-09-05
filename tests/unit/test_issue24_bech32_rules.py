"""issue #24 地址校验测试：witness version/program 长度/编码类型全规则。

- 有效地址：BIP173/BIP350 官方向量 + 主网 P2PKH/P2SH
- 错误 checksum、非法 witness version、非法 program 长度、错误编码类型
  （v0 用 bech32m / v1+ 用 bech32）均拒绝
- Base58Check payload 长度与主网 version byte 校验
"""
from __future__ import annotations

import hashlib

import pytest

from backend.core.btc_address import (
    _BECH32M_CONST,
    _bech32_hrp_expand,
    _bech32_polymod,
    _BECH32_CHARSET,
    encode_bech32m_address,
    validate_btc_address,
)


def _encode_with_const(program: bytes, version: int, use_m: bool) -> str:
    """按指定编码常数（bech32/bech32m）构造地址，用于错误编码类型用例。"""
    from backend.core.btc_address import _convertbits

    data = [version] + _convertbits(list(program), 8, 5)
    values = _bech32_hrp_expand("bc") + data
    poly = _bech32_polymod(values + [0] * 6)
    poly ^= _BECH32M_CONST if use_m else 1
    checksum = [(poly >> 5 * (5 - i)) & 31 for i in range(6)]
    return "bc1" + "".join(_BECH32_CHARSET[d] for d in data + checksum)


VALID = [
    # BIP173 官方向量：v0 P2WPKH / P2WSH
    "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
    "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3",
    # BIP350 官方向量：v1（40 字节）/ v2（16 字节）
    "bc1pw508d6qejxtdg4y5r3zarvary0c5xw7kw508d6qejxtdg4y5r3zarvary0c5xw7kt5nd6y",
    "bc1zw508d6qejxtdg4y5r3zarvaryvaxxpcs",
    "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4",  # 全大写合法
    "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",           # P2PKH genesis
    "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy",           # P2SH
]


class TestValidAddresses:
    @pytest.mark.parametrize("address", VALID)
    def test_accepted(self, address):
        assert validate_btc_address(address) is None

    def test_encoder_roundtrip_all_versions(self):
        """编码器输出必须通过校验（v0 bech32 / v1+ bech32m，issue #24）。"""
        for version in range(0, 17):
            program = hashlib.sha256(f"prog-{version}".encode()).digest()[
                :20 if version == 0 else 8 + version]
            addr = encode_bech32m_address(program, witness_version=version)
            assert validate_btc_address(addr) is None, (version, addr)


class TestInvalidAddresses:
    def test_bad_checksum(self):
        assert validate_btc_address(
            "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5") is not None

    def test_witness_version_31(self):
        addr = encode_bech32m_address(bytes(range(20)), witness_version=31)
        err = validate_btc_address(addr)
        assert err is not None and "version 31" in err

    def test_v0_program_1_byte(self):
        err = validate_btc_address(encode_bech32m_address(b"x", 0))
        assert err is not None and "20 or 32" in err

    def test_v0_program_21_bytes(self):
        err = validate_btc_address(encode_bech32m_address(bytes(21), 0))
        assert err is not None and "20 or 32" in err

    def test_v1_program_too_short(self):
        err = validate_btc_address(encode_bech32m_address(b"x", 1))
        assert err is not None and "out of" in err

    def test_v1_program_too_long(self):
        err = validate_btc_address(encode_bech32m_address(bytes(41), 1))
        assert err is not None and "out of" in err

    def test_v0_with_bech32m_encoding(self):
        addr = _encode_with_const(bytes(20), version=0, use_m=True)
        err = validate_btc_address(addr)
        assert err is not None and "version 0 must use bech32" in err

    def test_v1_with_bech32_encoding(self):
        addr = _encode_with_const(bytes(20), version=1, use_m=False)
        err = validate_btc_address(addr)
        assert err is not None and "must use bech32m" in err

    def test_mixed_case_rejected(self):
        err = validate_btc_address(
            "BC1QW508d6QEJxTDG4y5R3ZaRVARY0C5XW7KV8F3T4")
        assert err is not None and "mixed-case" in err

    def test_bad_base58_checksum(self):
        err = validate_btc_address("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb")
        assert err is not None and "checksum" in err

    @staticmethod
    def _base58_encode(raw: bytes) -> str:
        alphabet = ("123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
                    "abcdefghijkmnopqrstuvwxyz")
        num = int.from_bytes(raw, "big")
        addr = ""
        while num:
            addr = alphabet[num % 58] + addr
            num //= 58
        return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + addr

    def test_base58_wrong_payload_length(self):
        # 合法 base58check 但 24 字节（hash160 只有 19 字节）
        from backend.core.btc_address import _verify_base58check

        raw = b"\x00" + hashlib.new("ripemd160", b"x").digest()[:19]
        checksum = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
        err = _verify_base58check(self._base58_encode(raw + checksum))
        assert err is not None and "payload length" in err

    def test_base58_unsupported_version_byte(self):
        # 合法 base58check、长度 25，但 version byte 0x30 非主网 P2PKH/P2SH
        from backend.core.btc_address import _verify_base58check

        raw = b"\x30" + hashlib.new("ripemd160", b"x").digest()[:20]
        checksum = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
        err = _verify_base58check(self._base58_encode(raw + checksum))
        assert err is not None and "version byte" in err

    def test_testnet_prefix_rejected_as_unrecognized(self):
        # 'm'/'n' 开头（testnet P2PKH）在进入 base58 分支前即被拒
        err = validate_btc_address("mhA7W5WukoNbETqZ2tuLf3mWCoJRevLJZC")
        assert err is not None and "unrecognized" in err

    def test_non_empty_string_required(self):
        assert validate_btc_address("") is not None
        assert validate_btc_address(None) is not None
        assert validate_btc_address("not-an-address") is not None
