"use client";
// 分阶段分析进度条（issue #40）：四个阶段状态可见（待执行/进行中/完成），
// 失败呈现失败态；阶段数据由 worker 写 Redis、经 /judgments/{id} 轮询下发。
import { useAnalysisStore } from "@/store/analysis";

const STAGES = [
  { key: "building_subgraph", label: "构建子图 BFS" },
  { key: "retrieval_topk", label: "混合检索 Top-K" },
  { key: "wl_rerank", label: "WL kernel 精排" },
  { key: "llm_judging", label: "LLM 结构化判断" },
] as const;

export default function AnalysisStages() {
  const status = useAnalysisStore((s) => s.status);
  const stage = useAnalysisStore((s) => s.stage);

  // idle 未开始 / completed 终态由 VerdictCard 承接，进度条不再占用版面
  if (status === "idle" || status === "completed") return null;

  const currentIdx = stage ? STAGES.findIndex((s) => s.key === stage) : -1;
  const failed = status === "failed";
  // 失败但尚未上报任何阶段（如排队即被回收）：首个阶段标失败
  const failedIdx = failed ? Math.max(currentIdx, 0) : -1;

  return (
    <div className="rounded-xl border border-pt-line bg-pt-panel p-4" data-testid="analysis-stages">
      <p className="text-[10px] font-semibold uppercase tracking-[0.2em] text-pt-muted">
        分析流程
      </p>
      <ol className="mt-3 space-y-2">
        {STAGES.map((s, i) => {
          const done = i < currentIdx;
          const active = status === "processing" && i === currentIdx;
          const isFailed = failed && i === failedIdx;
          return (
            <li key={s.key} className="flex items-center gap-2 font-mono text-xs">
              <span
                aria-hidden
                className={
                  done ? "text-[#3a4250]"
                  : isFailed ? "text-red-400"
                  : active ? "animate-pulse text-pt-amber"
                  : "text-pt-faint"
                }
              >
                {done ? "✓" : isFailed ? "✕" : active ? "●" : "○"}
              </span>
              <span
                className={
                  done ? "text-pt-faint"
                  : isFailed ? "text-red-400"
                  : active ? "text-pt-ink"
                  : "text-pt-faint"
                }
              >
                {s.label}
                {isFailed && <span className="ml-2 font-sans">失败</span>}
                {active && s.key === "llm_judging" && (
                  <span className="ml-2 font-sans text-[10px] text-pt-muted">
                    LLM 推理耗时较长，请耐心等待
                  </span>
                )}
              </span>
            </li>
          );
        })}
      </ol>
      <p className="mt-3 font-mono text-[10px] tracking-widest text-pt-faint">
        {failed ? "分析失败" : status === "queued" ? "排队中，等待分析资源…" : "进行中"}
      </p>
    </div>
  );
}
