"use client";
// 分阶段分析进度条（issue #40 / 后续验收反馈）：
// - 横向布局，置于子图与 VerdictCard 上方（跨两栏）
// - 流势推进：阶段逐个完成（每 ~400ms 前进一格），即使后端一次轮询
//   已跨过多个阶段，也不一次性对多个流程打勾
// - 阶段数据由 worker 写 Redis、经 /judgments/{id} 轮询下发
import { useEffect, useState } from "react";
import { useAnalysisStore } from "@/store/analysis";

const STAGES = [
  { key: "building_subgraph", label: "子图构建 BFS" },
  { key: "retrieval_topk", label: "混合检索 Top-K" },
  { key: "wl_rerank", label: "WL kernel 精排" },
  { key: "llm_judging", label: "LLM 结构化判断" },
] as const;

const STEP_MS = 400; // 流势步进间隔
const DONE_HIDE_MS = 900; // 全部完成后停留时长，随后让位给 VerdictCard

export default function AnalysisStages() {
  const status = useAnalysisStore((s) => s.status);
  const stage = useAnalysisStore((s) => s.stage);
  // 流势：已显示为「完成」的阶段数，逐格推进到目标
  const [progress, setProgress] = useState(0);
  const [gone, setGone] = useState(false);

  const targetIdx = stage ? STAGES.findIndex((s) => s.key === stage) : -1;
  // completed → 目标推进到全部完成（依次打勾后隐藏）；失败 → 停在失败阶段
  const target = status === "completed" ? STAGES.length : targetIdx;

  // 重试/重新分析：回到排队态且尚无阶段上报 → 归零重来
  useEffect(() => {
    if (status === "queued" && targetIdx === -1) {
      setProgress(0);
      setGone(false);
    }
  }, [status, targetIdx]);

  // 流势推进：每次只前进一格
  useEffect(() => {
    if (gone || status === "failed" || progress >= target) return;
    const t = setTimeout(() => setProgress((p) => p + 1), STEP_MS);
    return () => clearTimeout(t);
  }, [progress, target, status, gone]);

  // 全部打勾后短暂停留再隐藏
  useEffect(() => {
    if (status === "completed" && progress >= STAGES.length) {
      const t = setTimeout(() => setGone(true), DONE_HIDE_MS);
      return () => clearTimeout(t);
    }
  }, [status, progress]);

  if (status === "idle" || gone) return null;

  // 失败：失败阶段之前的阶段如实显示已完成（事实呈现，不做流势动画）
  const failedIdx = status === "failed" ? Math.max(targetIdx, 0) : -1;

  return (
    <div
      className="rounded-xl border border-pt-line bg-pt-panel px-4 py-3"
      data-testid="analysis-stages"
    >
      <div className="flex items-center">
        {STAGES.map((s, i) => {
          const done = failedIdx >= 0 ? i < failedIdx : i < progress;
          const active = failedIdx < 0 && status === "processing" && i === progress;
          const isFailed = failedIdx >= 0 && i === failedIdx;
          return (
            <div key={s.key} className="flex min-w-0 flex-1 items-center last:flex-none">
              <span
                aria-hidden
                className={
                  isFailed ? "text-red-400"
                  : done ? "text-pt-amber"
                  : active ? "animate-pulse text-pt-amber"
                  : "text-pt-faint"
                }
              >
                {isFailed ? "✕" : done ? "✓" : active ? "●" : "○"}
              </span>
              <span
                className={`ml-1.5 whitespace-nowrap font-mono text-[11px] ${
                  isFailed ? "text-red-400"
                  : done ? "text-pt-amber-hi"
                  : active ? "text-pt-ink"
                  : "text-pt-faint"
                }`}
              >
                {s.label}
                {isFailed && <span className="ml-1 font-sans">失败</span>}
              </span>
              {i < STAGES.length - 1 && (
                <span
                  aria-hidden
                  className={`mx-3 h-px min-w-4 flex-1 ${
                    done || isFailed ? "bg-pt-amber/60" : "bg-pt-line"
                  }`}
                />
              )}
            </div>
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
    </div>
  );
}
