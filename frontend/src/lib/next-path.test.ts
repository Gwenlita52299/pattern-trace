// issue #70 回归：传给 router.push() 的必须是规范化站内路径。
// 参数化覆盖 javascript:/data:/绝对 URL/协议相对/反斜杠/编码控制字符变体。
import { describe, expect, it } from "vitest";

import { sanitizeNextPath } from "@/lib/next-path";

describe("sanitizeNextPath", () => {
  it("合法站内路径原样放行", () => {
    expect(sanitizeNextPath("/cases")).toBe("/cases");
    expect(sanitizeNextPath("/cases/abc-123")).toBe("/cases/abc-123");
    expect(sanitizeNextPath("/cases?id=42")).toBe("/cases?id=42");
  });

  it.each([
    ["javascript:alert(document.domain)"],
    ["JavaScript:alert(1)"],
    ["data:text/html,<script>"],
    ["https://evil.example/phish"],
    ["//evil.example"],
    ["/\\evil.example"],
    ["\\\\evil.example"],
    ["cases"], // 缺少开头 /
    [" /cases"],
    ["/cases\n?x=1"], // 控制字符（URL 编码 %0a 解码后的形态）
    ["/cas\u0000es"],
    [""],
  ])("危险输入 %j 回退到 /cases", (raw) => {
    expect(sanitizeNextPath(raw)).toBe("/cases");
  });

  it("null/undefined 回退到默认页", () => {
    expect(sanitizeNextPath(null)).toBe("/cases");
    expect(sanitizeNextPath(undefined)).toBe("/cases");
  });
});
