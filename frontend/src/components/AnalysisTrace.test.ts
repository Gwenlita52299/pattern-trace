// issue #73：阶段详情面板的可读性纯函数单测（耗时格式化 / 元数据折叠）。
// 组件渲染依赖 store 与网络，这里守护用户实际看到的文本口径。
import { describe, expect, it } from "vitest";

import { STATUS_ICON, formatDuration, metaEntries } from "@/components/AnalysisTrace";
import { TRACE_SPAN_LABELS } from "@/store/analysis";

describe("formatDuration（sub-1s 用毫秒，更长用秒）", () => {
  it("毫秒量级保持 ms", () => {
    expect(formatDuration(0)).toBe("0 ms");
    expect(formatDuration(999)).toBe("999 ms");
  });

  it("秒级以上换算并保留两位", () => {
    expect(formatDuration(1000)).toBe("1.00 s");
    expect(formatDuration(4512)).toBe("4.51 s");
  });

  it("未知耗时显示占位而不是 0（运行中的 span 不应被读成瞬时完成）", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
  });
});

describe("metaEntries（计数/版本原样展示，长值截断）", () => {
  it("过滤空值，保留计数与版本号", () => {
    const entries = metaEntries({
      nodes: 12,
      edges: 0,
      model: "deepseek-chat",
      missing: null,
      empty: "",
    });
    expect(entries).toContainEqual(["nodes", "12"]);
    expect(entries).toContainEqual(["edges", "0"]);
    expect(entries).toContainEqual(["model", "deepseek-chat"]);
    expect(entries.map(([k]) => k)).not.toContain("missing");
    expect(entries.map(([k]) => k)).not.toContain("empty");
  });

  it("对象值序列化后截断，避免撑破行宽", () => {
    const [entry] = metaEntries({ usage: { total_tokens: 1673 } });
    expect(entry[1]).toContain("total_tokens");
    const [long] = metaEntries({ blob: "x".repeat(500) });
    expect(long[1].endsWith("…")).toBe(true);
    expect(long[1].length).toBeLessThanOrEqual(81);
  });
});

describe("阶段展示映射", () => {
  it("后端 span 名都有中文文案（无裸英文 key 暴露给用户）", () => {
    for (const name of [
      "analysis",
      "building_subgraph",
      "esplora_fetch",
      "retrieval_topk",
      "embedding_call",
      "wl_rerank",
      "llm_judging",
      "llm_call",
    ]) {
      expect(TRACE_SPAN_LABELS[name]).toBeTruthy();
    }
  });

  it("失败/跳过/完成有各自的图标（色 + 图标双编码）", () => {
    expect(STATUS_ICON.completed).not.toBe(STATUS_ICON.failed);
    expect(STATUS_ICON.skipped).not.toBe(STATUS_ICON.completed);
  });
});
