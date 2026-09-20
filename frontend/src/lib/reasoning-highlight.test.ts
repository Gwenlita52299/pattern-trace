// 描述文本地址/tx 识别单测（Verdict：不出框 + 加粗高亮的前提）。
import { describe, expect, it } from "vitest";

import { tokenizeReasoning } from "@/lib/reasoning-highlight";

const ADDR = "bc1pewmn7vcp5rjmvnuwd5aqy9s9j9yf6z9yqncjv38l7n4e6guq4v6qn2gghz";
const TXID = "02b4aa6b3c9ed631afe44601ac638ae831cfc01d08a16a1efa2f650a1e96ac6b";

describe("tokenizeReasoning", () => {
  it("识别 bech32 地址与 txid，并缩短展示（完整值留 value）", () => {
    const tokens = tokenizeReasoning(`sends to ${ADDR} and ${TXID}.`);
    const kinds = tokens.map((t) => t.type);
    expect(kinds).toEqual(["text", "addr", "text", "tx", "text"]);
    const addr = tokens.find((t) => t.type === "addr")!;
    expect(addr.value).toBe(ADDR);
    expect(addr.display).toBe("bc1pewmn7v…gghz");
    const tx = tokens.find((t) => t.type === "tx")!;
    expect(tx.display).toBe("02b4aa6b3c…ac6b");
  });

  it("带 addr:/tx: 前缀与 legacy 地址同样识别", () => {
    const tokens = tokenizeReasoning(
      `edge to addr:${ADDR}, tx:${TXID}; legacy 1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2.`);
    const kinds = tokens.filter((t) => t.type !== "text").map((t) => t.type);
    expect(kinds).toEqual(["addr", "tx", "addr"]);
    const legacy = tokens.filter((t) => t.type === "addr").at(-1)!;
    expect(legacy.value).toContain("1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2");
  });

  it("以 1/3 开头的 64 位 hex 不被 legacy 正则部分匹配", () => {
    const txid = "1" + "a".repeat(63);
    const tokens = tokenizeReasoning(`tx ${txid} end`);
    expect(tokens.map((t) => t.type)).toEqual(["text", "tx", "text"]);
    expect(tokens[1].value).toBe(txid);
  });

  it("纯文本原样（无 token 时不改变文本）", () => {
    const text = "peel-and-return structure with no identifiers";
    const tokens = tokenizeReasoning(text);
    expect(tokens).toHaveLength(1);
    expect(tokens[0]).toMatchObject({ type: "text", display: text });
  });
});
