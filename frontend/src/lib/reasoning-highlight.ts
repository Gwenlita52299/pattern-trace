// issue #77 后续：Verdict 描述文本里的地址/交易 ID 识别与高亮（纯函数）。
// 用户反馈：confidence 下的描述里嵌完整地址/txid → 撑破边框且无层级。
import { shortId } from "@/lib/evidence-display";

export interface ReasoningToken {
  type: "text" | "addr" | "tx";
  value: string;   // 原始 token（完整值，供 title/复制）
  display: string; // 展示值（addr/tx 缩短，text 原样）
}

// 顺序关键：64 位 hex（txid）必须优先于 legacy 地址 —— 以 1/3 开头的
// txid 会被 legacy 正则部分匹配，先用 \b[a-f0-9]{64}\b 整体吃掉。
const TOKEN_RE = new RegExp(
  [
    "(?:tx:)?\\b[a-fA-F0-9]{64}\\b",                              // txid
    "(?:addr:)?(?:bc1[a-z0-9]{8,62}"                              // bech32
      + "|[13][a-km-zA-HJ-NP-Z1-9]{25,39})",                      // legacy
  ].join("|"),
  "g",
);

/** 把描述文本切成 普通文本 / 地址 / 交易 三类 token（地址与 tx 缩短展示）。 */
export function tokenizeReasoning(text: string): ReasoningToken[] {
  const tokens: ReasoningToken[] = [];
  let last = 0;
  for (const m of text.matchAll(TOKEN_RE)) {
    const start = m.index ?? 0;
    if (start > last) {
      tokens.push({ type: "text", value: text.slice(last, start),
                    display: text.slice(last, start) });
    }
    const raw = m[0];
    const type: ReasoningToken["type"] =
      /^(tx:)?[a-fA-F0-9]{64}$/.test(raw) ? "tx" : "addr";
    tokens.push({ type, value: raw, display: shortId(raw) });
    last = start + raw.length;
  }
  if (last < text.length) {
    tokens.push({ type: "text", value: text.slice(last),
                  display: text.slice(last) });
  }
  return tokens;
}

export interface ConclusionSplit {
  body: string;       // 结构描述部分
  conclusion: string; // 末尾判断/结论句（高亮展示）
}

/**
 * 拆分描述为「结构描述 + 末尾结论」——LLM 按 prompt 先叙述结构、
 * 最后给判断依据与结论；结论句单独高亮（用户要求"较重要的结论也高亮"）。
 *
 * 句边界要求标点后跟空白/结尾，避免把 0.93 这类小数误当句号。
 */
export function splitConclusion(text: string): ConclusionSplit {
  const trimmed = text.trim();
  const matches = [...trimmed.matchAll(/[.!?](?:\s+|$)/g)];
  if (matches.length === 0) {
    return { body: trimmed, conclusion: "" };
  }
  const last = matches[matches.length - 1];
  const lastEnd = (last.index ?? 0) + last[0].length;
  const tail = trimmed.slice(lastEnd).trim();
  if (tail) { // 末尾还有未以句号收尾的内容 → 它即结论
    return { body: trimmed.slice(0, lastEnd).trim(), conclusion: tail };
  }
  // 以句号结尾：最后一句（含标点）为结论；单句文本整体视作结论
  const prev = matches.length >= 2 ? matches[matches.length - 2] : null;
  const cut = prev ? (prev.index ?? 0) + prev[0].length : 0;
  return {
    body: trimmed.slice(0, cut).trim(),
    conclusion: trimmed.slice(cut).trim(),
  };
}
