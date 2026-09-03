"""OP_RETURN 脚本解析器 — backend/detection（issue #4）。

**Parser 只负责解析，不负责协议判断。** 输入一笔交易（dict 或 SimpleNamespace），
输出该交易中所有 OP_RETURN 输出的 pushdata 载荷（``OpReturnPayload`` 列表）。

设计要点：
- 保留 Esplora 的 ``scriptpubkey / scriptpubkey_asm / scriptpubkey_type`` 与 vout 索引。
- 识别 ``scriptpubkey_type == "op_return"`` 的输出（兜底：asm 以 ``OP_RETURN`` 开头 /
  raw script 首字节为 ``0x6a``）。
- 支持直接 pushdata（``0x01..0x4b``）、``OP_PUSHDATA1``（0x4c）、``OP_PUSHDATA2``（0x4d）、
  ``OP_PUSHDATA4``（0x4e）。
- 支持多个 OP_RETURN vout 与单脚本内多个 payload。
- 对空 payload、非法 hex、截断脚本返回``valid=False`` 的 ``malformed/unknown`` 结果，
  不抛异常、不中断解析其余输出。
- 保留原始脚本与 payload 作为审计证据（``scriptpubkey`` / ``payload_hex``）。
"""
from __future__ import annotations

from .models import OpReturnPayload, PARSER_VERSION

# Bitcoin script opcodes
OP_RETURN = 0x6A
OP_PUSHDATA1 = 0x4C
OP_PUSHDATA2 = 0x4D
OP_PUSHDATA4 = 0x4E

# 兼容多个 pushdata 单元：非 push（非 0x01..0x4b 与非 0x4c/0x4d/0x4e）一律跳过
MAX_PUSHDATA_LEN = 0x4B


def _get(obj, key, default=None):
    """兼容 dict 与 SimpleNamespace / 任意对象（live provider / fixture）。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _elem(elem, key, default=None):
    if isinstance(elem, dict):
        return elem.get(key, default)
    return getattr(elem, key, default)


def _looks_like_op_return(script_bytes: bytes, scriptpubkey_asm: str | None,
                          scriptpubkey_type: str | None) -> bool:
    # Esplora 的 scriptpubkey_type 是权威分类：显式设了但非 op_return（如 pegout/其他）
    # 时，绝不按 OP_RETURN 处理，即使 raw 脚本首字节恰为 0x6a。
    if scriptpubkey_type:
        return scriptpubkey_type == "op_return"
    if scriptpubkey_asm:
        return scriptpubkey_asm.strip().upper().startswith("OP_RETURN")
    return bool(script_bytes) and script_bytes[0] == OP_RETURN


def _parse_pushdata(script_bytes: bytes) -> tuple[list[bytes], str | None, int]:
    """解析 raw script 字节，返回 (payloads, error, pushdata_count)。

    只提取 push 操作的数据；遇到长度声明超出剩余字节视为截断（truncated）。
    """
    payloads: list[bytes] = []
    error: str | None = None
    n = len(script_bytes)
    i = 0
    push_count = 0
    while i < n:
        op = script_bytes[i]
        i += 1
        if op == OP_RETURN:
            continue
        if 0x01 <= op <= MAX_PUSHDATA_LEN:
            length = op
            if i + length > n:
                error = "truncated"
                break
            payloads.append(script_bytes[i:i + length])
            push_count += 1
            i += length
        elif op == OP_PUSHDATA1:
            if i + 1 > n:
                error = "truncated"
                break
            length = script_bytes[i]
            i += 1
            if i + length > n:
                error = "truncated"
                break
            payloads.append(script_bytes[i:i + length])
            push_count += 1
            i += length
        elif op == OP_PUSHDATA2:
            if i + 2 > n:
                error = "truncated"
                break
            length = int.from_bytes(script_bytes[i:i + 2], "little")
            i += 2
            if i + length > n:
                error = "truncated"
                break
            payloads.append(script_bytes[i:i + length])
            push_count += 1
            i += length
        elif op == OP_PUSHDATA4:
            if i + 4 > n:
                error = "truncated"
                break
            length = int.from_bytes(script_bytes[i:i + 4], "little")
            i += 4
            if i + length > n:
                error = "truncated"
                break
            payloads.append(script_bytes[i:i + length])
            push_count += 1
            i += length
        else:
            # 非 push 操作码（OP_DROP / OP_1 等）：跳过，不视为数据
            continue
    return payloads, error, push_count


class OpReturnParser:
    """OP_RETURN 脚本解析器。无状态；版本号暴露给审计证据。"""

    version = PARSER_VERSION

    def parse_vout(self, vout, index: int) -> list[OpReturnPayload]:
        """解析单个 vout；非 OP_RETURN / 无脚本 → 空列表。"""
        scriptpubkey = _elem(vout, "scriptpubkey") or ""
        scriptpubkey_asm = _elem(vout, "scriptpubkey_asm") or ""
        scriptpubkey_type = _elem(vout, "scriptpubkey_type") or ""

        # 无脚本或明确非 op_return（且 asm/首字节都不像 op_return）→ 不含解析目标
        if not scriptpubkey and not scriptpubkey_asm and not scriptpubkey_type:
            return []

        try:
            script_bytes = bytes.fromhex(scriptpubkey) if scriptpubkey else b""
        except (ValueError, TypeError):
            script_bytes = b""
            # scriptpubkey 存在但非合法 hex → malformed（invalid_hex），保留证据
            results: list[OpReturnPayload] = []
            if _looks_like_op_return(b"", scriptpubkey_asm, scriptpubkey_type):
                results.append(OpReturnPayload(
                    vout=index, valid=False, error="invalid_hex",
                    payload=b"", payload_hex="",
                    scriptpubkey=scriptpubkey, scriptpubkey_asm=scriptpubkey_asm,
                    scriptpubkey_type=scriptpubkey_type, pushdata_count=0,
                ))
            return results

        if not _looks_like_op_return(script_bytes, scriptpubkey_asm, scriptpubkey_type):
            return []

        payloads, error, push_count = _parse_pushdata(script_bytes)

        results: list[OpReturnPayload] = []
        if error is not None:
            # 截断 / 解析异常 → malformed（valid=False），保留证据与错误原因
            results.append(OpReturnPayload(
                vout=index, valid=False, error=error,
                payload=b"", payload_hex="",
                scriptpubkey=scriptpubkey, scriptpubkey_asm=scriptpubkey_asm,
                scriptpubkey_type=scriptpubkey_type, pushdata_count=push_count,
            ))
            return results

        if not payloads:
            # 有 OP_RETURN 但无有效 pushdata（空 payload）→ malformed
            results.append(OpReturnPayload(
                vout=index, valid=False, error="empty_payload",
                payload=b"", payload_hex="",
                scriptpubkey=scriptpubkey, scriptpubkey_asm=scriptpubkey_asm,
                scriptpubkey_type=scriptpubkey_type, pushdata_count=0,
            ))
            return results

        for p in payloads:
            results.append(OpReturnPayload(
                vout=index, valid=True, error=None,
                payload=p, payload_hex=p.hex(),
                scriptpubkey=scriptpubkey, scriptpubkey_asm=scriptpubkey_asm,
                scriptpubkey_type=scriptpubkey_type, pushdata_count=push_count,
            ))
        return results

    def parse_tx(self, tx) -> list[OpReturnPayload]:
        """解析一笔交易的所有 OP_RETURN vout（多个 vout / 多个 payload 展平）。"""
        payloads: list[OpReturnPayload] = []
        outputs = _get(tx, "outputs") or []
        for idx, vout in enumerate(outputs):
            payloads.extend(self.parse_vout(vout, idx))
        return payloads

    def parse_pegouts(self, tx) -> list[tuple[int, dict]]:
        """单独提取 ``vout.pegout`` 子对象（Liquid/Elements 通道，不混入 OP_RETURN）。"""
        pegouts: list[tuple[int, dict]] = []
        outputs = _get(tx, "outputs") or []
        for idx, vout in enumerate(outputs):
            peg = _elem(vout, "pegout")
            if peg:
                pegouts.append((idx, peg))
        return pegouts
