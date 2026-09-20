// 证据列表展示纯函数单测（Verdict 右侧：缩短显示 + 分类标记）。
import { describe, expect, it } from "vitest";

import { evidenceTag, shortId } from "@/lib/evidence-display";

describe("shortId（长地址缩短，完整值留给 title）", () => {
  it("保留前缀 + 首尾片段", () => {
    const addr = "addr:bc1pewmn7vcp5rjmvnuwd5aqy9s9j9yf6z9yqncjv38l7n4e6guq4v6qn2gghz";
    expect(shortId(addr)).toBe("addr:bc1pewmn7v…gghz");
  });

  it("tx 前缀同样处理；短 id 原样返回", () => {
    expect(shortId("tx:02b4aa6b3c9ed631afe44601ac638ae831cfc01d08a16a1efa2f650a1e96ac6b"))
      .toBe("tx:02b4aa6b3c…ac6b");
    expect(shortId("addr:16bSdwkgfer2WJwjW8vdK8Y6DsXmY9mHf4"))
      .toBe("addr:16bSdwkgfe…mHf4");
    expect(shortId("addr:short")).toBe("addr:short");
  });
});

describe("evidenceTag（特殊关联标记）", () => {
  const addr = "addr:bc1qz72gqtd6n2sswatala2gytqkdtaqmcnxzm66zd";
  it("跨链边关联 → crosschain（优先于 mixer）", () => {
    expect(evidenceTag(addr, [
      { source: addr, target: "tx:x", is_crosschain: true },
    ])).toBe("crosschain");
    expect(evidenceTag(addr, [
      { source: addr, target: "tx:x", is_crosschain: true, is_remixer: true },
    ])).toBe("crosschain");
  });

  it("仅混币关联 → mixer；无关联/非地址/空边 → null", () => {
    expect(evidenceTag(addr, [
      { source: addr, target: "tx:x", is_remixer: true },
    ])).toBe("mixer");
    expect(evidenceTag(addr, [{ source: "addr:other", target: "tx:x" }]))
      .toBeNull();
    expect(evidenceTag("tx:abc", [
      { source: addr, target: "tx:abc", is_crosschain: true },
    ])).toBeNull();
    expect(evidenceTag(addr, undefined)).toBeNull();
  });
});
