"use client";
// 分阶段分析进度条（参考 pattern_trace_demo.html 的 .pstep 设计）：
// - 横向数字圆点 + 连接线，active 脉冲光圈，完成后圆点填充换 ✓
// - 完成后常驻显示（不隐藏）；失败如实标 ✕ 红
// - 流势推进写入 store.stageProgress：分析页据此等进度条走满再呈现子图/结论
// - 阶段数据由 worker 写 Redis、经 /judgments/{id} 轮询下发
import { Fragment, useEffect } from "react";
import { ANALYSIS_STAGES, useAnalysisStore } from "@/store/analysis";

const STEP_MS = 400; // 流势步进间隔

export default function AnalysisStages() {
  const status = useAnalysisStore((s) => s.status);
  const stage = useAnalysisStore((s) => s.stage);
  const progress = useAnalysisStore((s) => s.stageProgress);

  const targetIdx = stage ? ANALYSIS_STAGES.findIndex((s) => s.key === stage) : -1;
  // completed → 目标推进到全部完成；失败 → 停在失败阶段
  const target = status === "completed" ? ANALYSIS_STAGES.length : targetIdx;

  // 流势推进：每次只前进一格；完成后停在常驻的全勾状态
  useEffect(() => {
    if (status === "failed" || progress >= target) return;
    const t = setTimeout(() => {
      useAnalysisStore.setState((s) => ({ stageProgress: s.stageProgress + 1 }));
    }, STEP_MS);
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
      {ANALYSIS_STAGES.map((s, i) => {
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
            {i < ANALYSIS_STAGES.length - 1 && (
              <span aria-hidden className="mx-3 h-[2px] min-w-6 flex-1 bg-pt-line" />
            )}
          </Fragment>
        );
      })}
      {/* LLM 阶段进行中的明确提示（验收标准） */}
      {status === "processing" && progress === ANALYSIS_STAGES.length - 1 && (
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
