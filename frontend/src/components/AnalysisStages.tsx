"use client";
// 分阶段分析进度条（参考 pattern_trace_demo.html 的 .pstep 设计）：
// - 横向数字圆点 + 连接线，active 脉冲光圈，完成后圆点填充换 ✓
// - 完成后常驻显示（不隐藏）；失败如实标 ✕ 红
// - 流势推进：阶段逐个完成（每 ~400ms 前进一格），即使后端一次轮询
//   已跨过多个阶段，也不一次性对多个流程打勾
// - 阶段数据由 worker 写 Redis、经 /judgments/{id} 轮询下发
import { Fragment, useEffect, useState } from "react";
import { useAnalysisStore } from "@/store/analysis";

const STAGES = [
  { key: "building_subgraph", label: "构建子图 BFS" },
  { key: "retrieval_topk", label: "混合检索 Top-K" },
  { key: "wl_rerank", label: "WL kernel 精排" },
  { key: "llm_judging", label: "LLM 结构化判断" },
] as const;

const STEP_MS = 400; // 流势步进间隔

export default function AnalysisStages() {
  const status = useAnalysisStore((s) => s.status);
  const stage = useAnalysisStore((s) => s.stage);
  // 流势：已显示为「完成」的阶段数，逐格推进到目标
  const [progress, setProgress] = useState(0);

  const targetIdx = stage ? STAGES.findIndex((s) => s.key === stage) : -1;
  // completed → 目标推进到全部完成；失败 → 停在失败阶段
  const target = status === "completed" ? STAGES.length : targetIdx;

  // 重试/重新分析：回到排队态且尚无阶段上报 → 归零重来
  useEffect(() => {
    if (status === "queued" && targetIdx === -1) setProgress(0);
  }, [status, targetIdx]);

  // 流势推进：每次只前进一格；到顶后停在常驻的全勾状态
  useEffect(() => {
    if (status === "failed" || progress >= target) return;
    const t = setTimeout(() => setProgress((p) => p + 1), STEP_MS);
    return () => clearTimeout(t);
  }, [progress, target, status]);

  if (status === "idle") return null;

  // 失败：失败阶段之前的阶段如实显示已完成（事实呈现，不做流势动画）
  const failedIdx = status === "failed" ? Math.max(targetIdx, 0) : -1;

  return (
    <div
      className="flex items-center rounded-xl border border-pt-line bg-pt-panel px-4 py-3"
      data-testid="analysis-stages"
    >
      {STAGES.map((s, i) => {
        const done = failedIdx >= 0 ? i < failedIdx : i < progress;
        const active = failedIdx < 0 && status === "processing" && i === progress;
        const isFailed = failedIdx >= 0 && i === failedIdx;
        return (
          <Fragment key={s.key}>
            <div
              className={`flex items-center gap-2 whitespace-nowrap ${
                active ? "font-semibold" : ""
              }`}
            >
              <span
                aria-hidden
                className={`flex h-[22px] w-[22px] items-center justify-center rounded-full border-2 font-mono text-[11px] transition-colors duration-300 ${
                  isFailed ? "border-red-400 text-red-400"
                  : done ? "border-pt-medium bg-pt-medium text-[#0e1116]"
                  : active ? "animate-stage-pulse border-pt-medium text-pt-medium"
                  : "border-[#2a3340] text-pt-faint"
                }`}
              >
                {isFailed ? "✕" : done ? "✓" : i + 1}
              </span>
              <span
                className={`font-mono text-[12px] ${
                  isFailed ? "text-red-400"
                  : done ? "text-pt-medium"
                  : active ? "text-pt-ink"
                  : "text-pt-faint"
                }`}
              >
                {s.label}
                {isFailed && <span className="ml-1 font-sans">失败</span>}
              </span>
            </div>
            {i < STAGES.length - 1 && (
              <span aria-hidden className="mx-3 h-[2px] min-w-6 flex-1 bg-pt-line" />
            )}
          </Fragment>
        );
      })}
      {/* LLM 阶段进行中的明确提示（验收标准） */}
      {status === "processing" && progress === STAGES.length - 1 && (
        <span className="ml-3 whitespace-nowrap font-sans text-[10px] text-pt-muted">
          LLM 推理耗时较长，请耐心等待
        </span>
      )}
      {status === "failed" && (
        <span className="ml-3 whitespace-nowrap font-sans text-[10px] text-red-400">
          分析失败
        </span>
      )}
      {status === "queued" && (
        <span className="ml-3 whitespace-nowrap font-sans text-[10px] text-pt-faint">
          排队中，等待分析资源…
        </span>
      )}
    </div>
  );
}
