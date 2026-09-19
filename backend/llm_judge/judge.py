"""LLM 结构化判断 + 防幻觉四重机制 — llm-judge-spec。

校验失败契约（LJ 用例统一口径）：
- 格式类失败（JSON 坏 / 字段缺失 / confidence 越界）→ 返回 None，触发通用重试；
- 规则类失败（evidence 引用越界 / flagged_no_pattern 语义违规）→ 抛
  JudgmentValidationError，
  携带具体非法 ID 列表供重试提示注入；编排层捕获计数，耗尽后上抛。
仅校验通过的结果才允许写入缓存。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass


class JudgmentValidationError(Exception):
    """evidence 引用越界或语义规则违反；invalid_ids 供重试提示使用。"""

    def __init__(self, reason: str, invalid_ids: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.invalid_ids = invalid_ids or []


# issue #72：risk_level 与匹配状态解耦（枚举重构，删除 no_match 档）。
# 匹配状态由 matched_pattern 是否为 null 表达；matched=null 且 risk∈{medium,high}
# = flagged_no_pattern（风险成立但无 KB 匹配），硬校验强制 action=review。
# confidence 全档统一为「对风险等级成立的确信度」，跨档可比。
VALID_RISK_LEVELS = {"high", "medium", "low"}
VALID_ACTIONS = {"freeze", "monitor", "review", "none"}

PROMPT_VERSION = "v10"  # v10：#83 方案 A——confidence 锚定引导（校准分数 → 数值档）
MAX_RETRIES = 3  # 总调用上限（首调 + 最多 2 次重试）
CACHE_TTL_SECONDS = 7 * 86400  # spec §5.3
BUILDER_VERSION = "gb-v1"

# spec §4 JSON Schema —— 与 System Prompt 联合约束输出
SCHEMA: dict = {
    "type": "object",
    "properties": {
        "risk_level": {"enum": ["high", "medium", "low"]},
        "matched_pattern": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
        "recommended_action": {"enum": ["freeze", "monitor", "review", "none"]},
    },
    "required": ["risk_level", "matched_pattern", "confidence",
                 "evidence", "reasoning", "recommended_action"],
}

# spec §3 System Prompt 全文（LJ-19 逐条断言其中的防幻觉规则）
SYSTEM_PROMPT = """You are a blockchain forensics analyst. Given a transaction subgraph and candidate \
money-laundering patterns, assess the risk level.

Rules:
1. Output ONLY valid JSON matching the provided schema.
2. Every ID in the "evidence" array MUST be an address node id \
("addr:...") from the input subgraph. Cite addresses only — never \
transaction or edge IDs. Cite AT MOST 8 addresses: choose the strongest \
evidence only (long evidence lists get truncated and fail validation).
3. Matching state is expressed ONLY by matched_pattern (null = no KB match), \
never by risk_level. If no pattern matches, judge the observed risk directly: \
risk_level="medium" or "high" for suspicious structure (mixer contact, anomalous \
behavior such as address-reuse returns, peel-and-return into coinjoin) with \
recommended_action="review"; risk_level="low" only when no suspicious structure \
is observed.
4. Do not hallucinate transaction IDs or addresses.
5. Confidence is a float between 0.0 and 1.0. It expresses certainty in \
the risk assessment, not in pattern matching. Ground it in evidence \
strength — do NOT collapse different situations to the same value: \
- matched pattern with strong calibration (final>=0.6 and wl>=0.4) \
→ 0.80-0.95;
- KB match with partial correspondence → 0.60-0.75;
- flagged_no_pattern (matched=null, medium/high) with concrete anomalous \
behavior (address-reuse return, peel-and-return into coinjoin) → 0.65-0.85;
- flagged_no_pattern with weak or generic suspicion → 0.40-0.60;
- low risk with no suspicious structure → 0.20-0.40.
6. reasoning must reference specific structural features of the input subgraph.
7. matched_pattern MUST be either null or the exact pattern_name of one of the \
CANDIDATE PATTERNS listed in the input. Never invent a pattern name."""

# issue #8：数据质量 degraded 时注入的提示——图不完整，须谨慎判断、说明局限、
# 不能把观察到的图当作完整链路。
DEGRADED_NOTE = (
    "The transaction graph is INCOMPLETE because some upstream data requests failed. "
    "Do not treat the observed graph as exhaustive. "
    "Explain the missing-data limitation and provide a cautious assessment. "
    "Lower confidence accordingly; prefer recommended_action='review' unless evidence "
    "is conclusive within the observed subgraph."
)


@dataclass
class JudgmentResult:
    risk_level: str
    matched_pattern: str | None
    confidence: float
    evidence: list[str]
    reasoning: str
    recommended_action: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> JudgmentResult:
        return cls(**json.loads(raw))


def build_cache_key(
    builder_version: str, address: str, subgraph_hash: str,
    model: str, prompt_version: str,
    mock_scenario: str | None = None,
) -> str:
    """缓存 key 含 builder_version（评审 P2-23）。

    issue #78：mock provider 的 per-judgment 场景必须进 key——不同
    scenario（invalid_evidence/no_match/正例）对同一子图的判定不同，
    不加会导致 E2E 场景间缓存互相污染。真实 provider scenario=None，
    key 与旧格式一致。
    """
    key = (f"{builder_version}:{address}:{subgraph_hash}"
           f":{model}:{prompt_version}")
    if mock_scenario:
        key += f":mock:{mock_scenario}"
    return key


def canonical_subgraph_hash(subgraph: dict) -> str:
    """规范化序列化 hash（架构师 P2-25）。

    消除三类表示差异：字段顺序（sort_keys）、label 展示信息（白名单剔除）、
    节点/边列表顺序（按 id 排序——图在集合语义上无序）；浮点统一 round(8)。
    """
    def _normalize(obj):
        if isinstance(obj, dict):
            allowed = {k: v for k, v in sorted(obj.items()) if k != "label"}
            return {k: _normalize(v) for k, v in allowed.items()}
        if isinstance(obj, list):
            items = [_normalize(x) for x in obj]
            if items and all(isinstance(x, dict) and "id" in x for x in items):
                items.sort(key=lambda x: x["id"])
            return items
        if isinstance(obj, float):
            return round(obj, 8)
        return obj

    normalized = json.dumps(_normalize(subgraph), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode()).hexdigest()


def _extract_json_object(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text.strip(), re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_and_validate(raw: str, valid_ids: set[str],
                       candidate_names: set[str] | None = None) -> JudgmentResult | None:
    """硬校验：schema 类失败返回 None（触发重试）；规则类失败抛异常。

    - JSON 不可解析 / required 缺失 / 枚举非法 / confidence ∉ [0,1] → None
    - evidence 引用 ⊄ 子图 ID 空间 → JudgmentValidationError(含非法 ID 列表)
    - risk 枚举非法（issue #72：{high,medium,low}）→ None
    - evidence 引用 ⊄ 子图 ID 空间 → JudgmentValidationError(含非法 ID 列表)
    - matched=null 且 risk∈{medium,high} 但 action≠review → 异常
      （flagged_no_pattern 口径，issue #72）
    - issue #26：matched_pattern 非 null 时必须是本次检索候选名称之一；
      候选为空时必须为 null。candidate_names=None 时跳过该检查（直调兼容）
    """
    data = _extract_json_object(raw)
    if data is None:
        return None

    required = ["risk_level", "matched_pattern", "confidence",
                "evidence", "reasoning", "recommended_action"]
    if any(k not in data for k in required):
        return None
    if data["risk_level"] not in VALID_RISK_LEVELS:
        return None
    if data["recommended_action"] not in VALID_ACTIONS:
        return None
    if isinstance(data["confidence"], bool) or \
            not isinstance(data["confidence"], (int, float)) or \
            not (0.0 <= data["confidence"] <= 1.0):
        return None
    if not isinstance(data["evidence"], list) or \
            not all(isinstance(e, str) for e in data["evidence"]):
        return None
    if data["matched_pattern"] is not None and not isinstance(data["matched_pattern"], str):
        return None

    invalid = sorted(set(data["evidence"]) - set(valid_ids))
    if invalid:  # 防幻觉核心：引用必须存在于子图快照（spec §5.2）
        raise JudgmentValidationError(
            f"evidence references IDs outside the subgraph snapshot: {invalid}",
            invalid_ids=invalid,
        )
    # issue #26：引用收口到本次检索候选——防提示注入/幻觉伪造匹配模式
    # （候选为空时 candidate_names 为空集，任何非 null 引用都会命中下面的 not in）
    matched = data["matched_pattern"]
    if matched is not None and candidate_names is not None \
            and matched not in candidate_names:
        raise JudgmentValidationError(
            f"matched_pattern {matched!r} is not among the retrieved "
            "candidate patterns; use an exact pattern_name from "
            "CANDIDATE PATTERNS or null",
            invalid_ids=[matched],
        )
    # issue #72：matched=null 且 risk∈{medium,high}（flagged_no_pattern）→
    # 风险成立但无 KB 匹配，必须上报人工复查，不得静默放行
    if data["matched_pattern"] is None \
            and data["risk_level"] in ("medium", "high") \
            and data["recommended_action"] != "review":
        raise JudgmentValidationError(
            'flagged_no_pattern (matched_pattern=null with risk '
            f'{data["risk_level"]}) requires recommended_action="review"')

    return JudgmentResult(**data)


def _view(obj, key, default=None):
    """dict / dataclass 双态取值（canonical dict 或 SubgraphResult）。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _plain(obj):
    """dataclass → dict，保证 canonical_subgraph_hash 可 JSON 序列化。"""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    return obj


def subgraph_valid_ids(subgraph) -> set[str]:
    """物证引用空间：仅地址节点 id（物证统一为 addr，tx/edge 不再可引用）。"""
    nodes = _view(subgraph, "nodes") or []
    return {_view(n, "id") for n in nodes
            if isinstance(_view(n, "id"), str)
            and _view(n, "id").startswith("addr:")}


def build_messages(subgraph, candidates, retry_note: str | None = None,
                   degraded: bool = False,
                   missing_branches: int = 0) -> list[dict]:
    """system prompt + 子图紧凑摘要 + 候选 pattern 列表（+ 重试错误提示 / degraded 说明）。"""
    nodes = _view(subgraph, "nodes") or []
    edges = _view(subgraph, "edges") or []
    lines = ["INPUT SUBGRAPH:"]
    lines.append(f"seed: {_view(subgraph, 'seed_address', '')}")
    if degraded:
        lines.append(f"DATA QUALITY: degraded (missing_branches={missing_branches})")
        lines.append("NOTE: " + DEGRADED_NOTE)
    for n in nodes:
        lines.append(f"node id={_view(n, 'id')} kind={_view(n, 'kind')}")
    for e in edges:
        flags = []
        if _view(e, "is_remixer"):
            flags.append("remixer")
        if _view(e, "is_crosschain"):
            flags.append(f"crosschain:{_view(e, 'op_return_protocol')}")
        if _view(e, "is_stopped_expansion"):
            flags.append("stopped")
        suffix = f" [{','.join(flags)}]" if flags else ""
        lines.append(f"edge id={_view(e, 'id')} "
                     f"{_view(e, 'source')}->{_view(e, 'target')}{suffix}")

    lines.append("\nCANDIDATE PATTERNS:")
    if not candidates:
        lines.append("(none retrieved)")
    else:
        # issue #71（方案 C）：校准信号用法——预计算的结构相似度分数，
        # 弱 wl（<0.3）提示主干结构弱匹配；中高分候选才值得结构对齐判断
        lines.append(
            "CALIBRATION LINE per candidate: final=blended ranking score, "
            "wl=WL-kernel structural similarity (scale-invariant, weak <0.3 "
            "suggests only partial structural correspondence), ov=output-edge "
            "multiset overlap, fp=fingerprint similarity, struct/sem=stage-1 "
            "structure/semantic channel scores. Use these to gauge how much "
            "of the pattern is actually present; compare the value backbone "
            "of small candidate topologies against the input, not exact "
            "node/edge counts. Reflect the strongest calibration evidence "
            "in confidence (rule 5).")
    for c in candidates:
        name = _view(c, "name")
        desc = _view(c, "description", "") or ""
        note = _view(c, "difference_note", None)
        source = _view(c, "source", "lazarus_confirmed")
        provenance = _view(c, "provenance", "confirmed")
        # issue #10：合成样本不是真实链上证据——必须显式携带来源性质，
        # 并阻止模型把候选自身当作区块链证据引用。
        line = f"pattern_name: {name}\n  source: {source}\n  provenance: {provenance}"
        if provenance != "confirmed":
            line += "\n  evidence_status: not_real_on_chain_evidence"
        # issue #71（方案 C）：精排校准信号透传——WL/overlap 分数已在检索侧
        # 算好（PatternCandidate），旧口径只给 description，judge 被 ingest
        # 模板的精确计数硬锚定，同簇变体（弱 WL 信号）被误判
        line += (
            "\n  calibration: final={:.3f} wl={:.3f} ov={:.3f} fp={:.3f} "
            "struct={:.3f} sem={:.3f}".format(
                _view(c, "similarity_score", 0.0) or 0.0,
                _view(c, "wl_kernel_score", 0.0) or 0.0,
                _view(c, "ov_score", 0.0) or 0.0,
                _view(c, "fp_score", 0.0) or 0.0,
                _view(c, "structural_similarity", 0.0) or 0.0,
                _view(c, "semantic_similarity", 0.0) or 0.0))
        line += f"\n  {desc}"
        if note:
            line += f"\n  difference vs input: {note}"
        lines.append(line)

    synthetic_any = any(
        _view(c, "provenance", "confirmed") != "confirmed" for c in candidates)
    if synthetic_any:
        lines.append(
            "\nNOTE: One or more candidates are SYNTHETIC structural templates. "
            "They do not represent a real on-chain transaction or a confirmed case. "
            "Do not cite the candidate itself as blockchain evidence.")

    user_content = "\n".join(lines)
    if retry_note:
        user_content += f"\n\n{retry_note}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


class InMemoryCache:
    """redis 兼容子集（get/setex）；无 Redis 的开发/测试环境降级用。"""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str | None:
        item = self._store.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at < time.monotonic():
            self._store.pop(key, None)
            return None
        return value

    def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        self._store[key] = (value, time.monotonic() + ttl_seconds)


_GENERIC_RETRY_NOTE = (
    "Your previous response was not valid JSON per the schema or violated field "
    "constraints. Output ONLY valid JSON matching the provided schema."
)


def retry_note_for_invalid_ids(invalid_ids: list[str]) -> str:
    return (
        f"Your previous response referenced invalid IDs: "
        f"[{', '.join(invalid_ids)}]. Only use IDs from the provided subgraph."
    )


class LLMJudge:
    """判断编排（spec §6）：缓存 → 构造消息 → 重试循环 → 仅成功才缓存。"""

    def __init__(self, client, cache=None, max_retries: int = MAX_RETRIES):
        self.client = client
        self.cache = cache if cache is not None else InMemoryCache()
        self.max_retries = max_retries
        self.last_thinking = ""  # 最近一次成功判断的 <think> 审计留痕（spec §7）

    async def judge(
        self,
        *,
        address: str,
        subgraph,
        candidates=(),
        candidate_names: set[str] | None = None,
        builder_version: str = BUILDER_VERSION,
        model: str = "",
        prompt_version: str = PROMPT_VERSION,
        temperature: float = 0.0,
        degraded: bool = False,
        missing_branches: int = 0,
    ) -> JudgmentResult:
        valid_ids = subgraph_valid_ids(subgraph)
        model = model or getattr(self.client, "model_name", "")
        # hash 输入统一转 plain dict（SubgraphResult 的 Node/Edge 是 dataclass）
        canon = subgraph if isinstance(subgraph, dict) else {
            "nodes": [_plain(n) for n in (_view(subgraph, "nodes") or [])],
            "edges": [_plain(e) for e in (_view(subgraph, "edges") or [])],
        }
        # hash 只依赖结构化字段（label 参与展示不影响 key，LJ-16）
        subgraph_hash = canonical_subgraph_hash(canon)
        # issue #78：mock provider 的 scenario 进缓存 key（防场景间污染）
        cache_key = build_cache_key(
            builder_version, address, subgraph_hash,
            model, prompt_version,
            mock_scenario=getattr(self.client, "scenario", None))

        cached = self.cache.get(cache_key)
        if cached is not None:  # LJ-07：命中即返回，不触达 LLM
            return JudgmentResult.from_json(cached)

        messages = build_messages(subgraph, candidates,
                                  degraded=degraded,
                                  missing_branches=missing_branches)
        last_error: JudgmentValidationError | None = None
        for attempt in range(self.max_retries):
            attempt_messages = messages
            if attempt > 0:  # LJ-04：重试请求附带上一轮的具体错误提示
                note = (retry_note_for_invalid_ids(last_error.invalid_ids)
                        if last_error and last_error.invalid_ids
                        else _GENERIC_RETRY_NOTE)
                attempt_messages = messages[:-1] + [
                    {"role": "user",
                     "content": messages[-1]["content"] + f"\n\n{note}"},
                ]
            raw = await self.client.complete(
                attempt_messages, json_schema=SCHEMA,
                temperature=temperature)
            thinking, cleaned = self.extract_thinking(raw)
            if thinking:
                # 审计留痕：<think> 内容随结果落 judgments.thinking（spec §7）
                self.last_thinking = thinking
            try:
                result = parse_and_validate(cleaned, valid_ids,
                                            candidate_names=candidate_names)
            except JudgmentValidationError as exc:
                last_error = exc
                continue
            if result is not None:
                self.cache.setex(cache_key, CACHE_TTL_SECONDS, result.to_json())
                return result
            last_error = JudgmentValidationError("response failed schema validation")

        raise last_error or JudgmentValidationError(
            f"judgment validation failed after {self.max_retries} attempts")

    @staticmethod
    def extract_thinking(raw_response: str) -> tuple[str, str]:
        """Qwen3 thinking mode 提取 — spec §7。"""
        match = re.search(r"<think>(.*?)</think>", raw_response, re.DOTALL)
        if match:
            return match.group(1).strip(), raw_response.replace(match.group(0), "").strip()
        return "", raw_response.strip()
