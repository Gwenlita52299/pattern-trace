// 图证据编码单测：跨链（thorchain 等）必须与普通停止扩展边区分——
// 回归守护「前端未标记 thorchain 跨链 tx」问题（琥珀 = 证据，唯一强调色）。
import { describe, expect, it } from "vitest";

import { edgeDashed, edgeStroke, terminalTone } from "@/components/GraphCanvas";

const AMBER = "#f0b429";
const STOP_GRAY = "#3a4250";
const DEFAULT_GRAY = "#2a3340";

describe("edgeStroke（证据边取色优先级）", () => {
  it("物证高亮最高优先", () => {
    expect(edgeStroke({ is_crosschain: true }, true)).toBe(AMBER);
    expect(edgeStroke({ is_stopped_expansion: true }, true)).toBe(AMBER);
  });

  it("跨链边用琥珀而非停止灰（核心：thorchain 不被淹没）", () => {
    expect(edgeStroke(
      { is_crosschain: true, is_stopped_expansion: true }, false)).toBe(AMBER);
  });

  it("混币边琥珀；普通停止灰；普通默认灰", () => {
    expect(edgeStroke(
      { is_remixer: true, is_stopped_expansion: true }, false)).toBe(AMBER);
    expect(edgeStroke({ is_stopped_expansion: true }, false)).toBe(STOP_GRAY);
    expect(edgeStroke({}, false)).toBe(DEFAULT_GRAY);
  });
});

describe("edgeDashed（仅非证据停止边虚线）", () => {
  it("证据边实线", () => {
    expect(edgeDashed(
      { is_crosschain: true, is_stopped_expansion: true })).toBe(false);
    expect(edgeDashed(
      { is_remixer: true, is_stopped_expansion: true })).toBe(false);
  });

  it("普通停止虚线；普通边实线", () => {
    expect(edgeDashed({ is_stopped_expansion: true })).toBe(true);
    expect(edgeDashed({})).toBe(false);
  });
});

describe("terminalTone（终止节点语气）", () => {
  it("mixer 优先于 crosschain，crosschain 与普通停止区分", () => {
    expect(terminalTone(true, true)).toBe("mixer");
    expect(terminalTone(false, true)).toBe("crosschain");
    expect(terminalTone(false, false)).toBe("stopped");
  });
});
