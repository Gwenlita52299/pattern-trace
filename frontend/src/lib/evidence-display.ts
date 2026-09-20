// issue #77 后续：Verdict 证据列表的展示辅助（纯函数，便于单测）。
// 用户反馈：右侧证据列表无重点、长地址撑破边框 → 缩短显示 + 分类标记。

/** 地址/交易 ID 缩短显示：保留前缀 + 首尾片段（完整值走 title/aria）。 */
export function shortId(eid: string, head = 10, tail = 4): string {
  const colon = eid.indexOf(":");
  const prefix = colon >= 0 ? eid.slice(0, colon + 1) : "";
  const body = colon >= 0 ? eid.slice(colon + 1) : eid;
  if (body.length <= head + tail + 1) return eid;
  return `${prefix}${body.slice(0, head)}…${body.slice(-tail)}`;
}

export interface EdgeLike {
  source: string;
  target: string;
  is_crosschain?: boolean;
  is_remixer?: boolean;
}

/**
 * 证据地址在子图中的特殊关联（决定图标标记）：
 * 跨链优先于混币（跨链是更外层的资金离链信号）。
 */
export function evidenceTag(
  eid: string,
  edges: EdgeLike[] | undefined,
): "crosschain" | "mixer" | null {
  if (!eid.startsWith("addr:") || !edges?.length) return null;
  const related = edges.filter((e) => e.source === eid || e.target === eid);
  if (related.some((e) => e.is_crosschain)) return "crosschain";
  if (related.some((e) => e.is_remixer)) return "mixer";
  return null;
}
