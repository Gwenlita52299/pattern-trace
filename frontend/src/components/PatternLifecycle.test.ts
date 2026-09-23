// issue #75：模式生命周期面板的纯函数单测（状态/来源文案、版本差异）。
import { describe, expect, it } from "vitest";

import {
  INDEX_LABELS,
  ORIGIN_LABELS,
  STATUS_LABELS,
  indexLabel,
  originLabel,
  revisionDiff,
  statusLabel,
} from "@/components/PatternLifecycle";
import type { PatternRevision } from "@/lib/api";

function rev(over: Partial<PatternRevision>): PatternRevision {
  return {
    id: 1, revision: 1, status: "active", origin: "ingest",
    index_status: "indexed", name: "p", source: "lazarus_confirmed",
    provenance: "confirmed", evidence_grade: "A", content_hash: "h1",
    ...over,
  };
}

describe("生命周期的可读文案", () => {
  it("生命周期/索引/来源都有中文标签，未知值原样回退", () => {
    expect(statusLabel("active")).toBe(STATUS_LABELS.active);
    expect(statusLabel("deprecated")).toBe(STATUS_LABELS.deprecated);
    expect(indexLabel("failed")).toBe(INDEX_LABELS.failed);
    expect(originLabel("rollback")).toBe(ORIGIN_LABELS.rollback);
    expect(statusLabel("weird")).toBe("weird");
    expect(originLabel("weird")).toBe("weird");
  });

  it("draft 明确显示为待审核（未审核不得上线）", () => {
    expect(STATUS_LABELS.draft).toContain("待审核");
  });
});

describe("revisionDiff（只列实际变化的项）", () => {
  it("内容一致的版本不产生差异行", () => {
    expect(revisionDiff(rev({}), rev({ revision: 2 }))).toEqual([]);
  });

  it("名称、描述、规模变化各自可见", () => {
    const base = { name: "p", description: "old", node_count: 2, edge_count: 1 };
    const target = rev({
      revision: 2, name: "renamed", description: "new text",
      canonical_subgraph: { nodes: [{}, {}, {}], edges: [{}, {}] },
    });
    const lines = revisionDiff(base, target);
    expect(lines.some((l) => l.includes("名称"))).toBe(true);
    expect(lines.some((l) => l.includes("描述"))).toBe(true);
    expect(lines.some((l) => l.includes("规模"))).toBe(true);
  });

  it("两侧都没有规模信息时不报规模差异（列表项场景）", () => {
    const lines = revisionDiff(rev({}), rev({ revision: 2 }));
    expect(lines.some((l) => l.includes("规模"))).toBe(false);
  });
});
