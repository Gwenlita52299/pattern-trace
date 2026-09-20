// issue #77：检索解释抽屉的纯函数（空态文案 / 来源徽标）单测。
// 组件整体渲染依赖 store/网络，这里只守护用户可见文案与双重区分逻辑。
import { describe, expect, it } from "vitest";

import {
  emptyReasonText,
  provenanceBadge,
} from "@/components/RetrievalExplanation";

describe("emptyReasonText（无候选时明确说明，而非空白）", () => {
  it("空子图 / 无召回 / 进行中 / 未记录 各自可读", () => {
    expect(emptyReasonText("empty_subgraph")).toContain("子图为空");
    expect(emptyReasonText("no_recall_match")).toContain("阈值");
    expect(emptyReasonText("analysis_in_progress")).toContain("进行中");
    expect(emptyReasonText("retrieval_not_recorded")).toContain("未记录");
  });

  it("未知原因回退为带原因的通用文案，空值回退为「无候选」", () => {
    expect(emptyReasonText("something_else")).toContain("something_else");
    expect(emptyReasonText(null)).toBe("无候选");
  });
});

describe("provenanceBadge（合成模板与真实证据双重区分）", () => {
  it("confirmed → 真实链上证据徽标", () => {
    const b = provenanceBadge("confirmed");
    expect(b.text).toContain("真实链上证据");
  });

  it("非 confirmed（synthetic 等）→ 明确标注非真实链上证据", () => {
    for (const p of ["synthetic", "e2e_fixture", "unknown"]) {
      expect(provenanceBadge(p).text).toContain("非真实链上证据");
    }
  });
});
