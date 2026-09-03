# Crosschain 检测规范 — backend/detection（issue #4）

目标：把 crosschain 判定从「预生成 txid→protocol 标签表」升级为**运行时、模块化、可单独
测试**的 OP_RETURN / peg-out 协议检测能力，并以稳定接口供 GraphBuilder 依赖。

## 1. 数据流

```
Esplora transaction -> OpReturnParser -> Protocol Decoder Registry -> CrosschainDetector -> GraphBuilder early-stop
```

- `OpReturnParser` 只解析，不判协议；从 Esplora 交易详情读取 OP_RETURN 相关字段。
- 协议 `Decoder` 各自独立，通过 `registry` 注册；OP_RETURN 通道与 Liquid/Elements
  `vout.pegout` 通道**语义隔离，不混淆**。
- `CrosschainDetector` 是唯一稳定的交易级检测入口，GraphBuilder 只依赖它，不新增协议分支。

## 2. 模块划分（backend/detection）

| 文件 | 职责 |
|---|---|
| `op_return.py` | `OpReturnParser`：解析 OP_RETURN 脚本/pushdata |
| `crosschain.py` | `CrosschainDetector`：总入口，输出 `Detection` |
| `models.py` | `CrosschainType` / `OpReturnPayload` / `PegoutEvidence` / `ProtocolMatch` / `Detection` |
| `protocols/registry.py` | `DecoderRegistry`：按通道登记/枚举 Decoder |
| `protocols/thorchain.py` | THORChain memo Decoder（OP_RETURN 通道） |
| `protocols/pegout.py` | Liquid/Elements peg-out Decoder（pegout 通道） |

## 3. OpReturnParser

- 保留 Esplora 的 `scriptpubkey` / `scriptpubkey_asm` / `scriptpubkey_type` 与 vout 索引。
- 识别 `scriptpubkey_type == "op_return"`；无 type 时以 asm `OP_RETURN` 前缀 / raw 首字节
  `0x6a` 兜底（显式非 op_return 的 type（如 `pegout`）绝不按 OP_RETURN 处理）。
- 支持直接 pushdata（`0x01..0x4b`）、`OP_PUSHDATA1/2/4`（`0x4c/0x4d/0x4e`）。
- 支持多 OP_RETURN vout 与单脚本多 payload。
- 空 payload / 非法 hex / 截断脚本 → 返回 `valid=False` 的 malformed 结果，不抛异常。

## 4. CrosschainDetector 判定规则

```python
Detection(type, is_crosschain, protocol, reason, evidence, parser_version,
          detector_version, decoder_version)
```

- 发现 OP_RETURN 不等于发现跨链；未知 OP_RETURN 不得设置 `is_crosschain=True`。
- 只有已注册且成功匹配的协议 Decoder 才能确认跨链。
- 不同 decoder 同时命中不同协议 → `ambiguous`（`is_crosschain=False`），默认不停止展开。
- 优先级（GraphBuilder 级联）：time window → CoinJoin → Crosschain → expand。

## 5. GraphBuilder 接线

命中后保持现有行为：`is_stopped_expansion=true`、`is_crosschain=true`、写入
`op_return_protocol`，不继续展开该交易输出，并保留 reason/evidence/version。
`crosschain_tx_set`（DB/夹具标签表）仅作为兜底保留，主判定由 Detector 承担。

## 6. Esplora / Fixture

- live provider 保留 `scriptpubkey` / `scriptpubkey_asm` / `scriptpubkey_type` /
  `scriptpubkey_address` / `value` 与可选 `pegout`。
- fixture 提供带原始 OP_RETURN 脚本的交易样本，覆盖已支持协议、unknown、malformed、
  多 OP_RETURN 与 CoinJoin/crosschain 同时命中；不依赖 CSV 即可运行。
