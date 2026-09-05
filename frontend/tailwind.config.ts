import type { Config } from "tailwindcss";

// 设计调性 token 见 docs/design-tone.md（grill-me 钉死版）：
// 冷灰炭底 + 琥珀=证据 · 数据等宽 · 警示极克制
const config: Config = {
  content: [
    "./src/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        pt: {
          bg: "#0e1116",
          panel: "#141920",
          "panel-2": "#181f28",
          line: "rgba(255,255,255,0.07)",
          ink: "#e6e9ef",
          muted: "#8b93a1",
          faint: "#4a5261",
          amber: "#f0b429",
          "amber-hi": "#ffd166",
          medium: "#5b9dff",
        },
      },
      fontFamily: {
        sans: [
          "-apple-system",
          "SF Pro Display",
          "PingFang SC",
          "system-ui",
          "sans-serif",
        ],
        mono: [
          "SF Mono",
          "JetBrains Mono",
          "Menlo",
          "monospace",
        ],
      },
    },
  },
  plugins: [],
};
export default config;
