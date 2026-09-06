// analysis store 轮询状态机单测（issue #57）：完成门控的 stageProgress
// 语义（首轮回调直接 completed → 跳满进度不重放动画；观察到过运行态则
// 保持当前进度由进度条组件逐格推进）、failed 态。
import { beforeEach, describe, expect, it, vi } from "vitest";

// api 模块打桩：pollJudgment 的取数路径
vi.mock("@/lib/api", () => ({ api: vi.fn() }));

import { api } from "@/lib/api";
import { ANALYSIS_STAGES, useAnalysisStore } from "@/store/analysis";

const apiMock = vi.mocked(api);

const START_INTERVAL_MS = 2_000;

function resetStore() {
  useAnalysisStore.setState({
    judgment: null,
    subgraph: null,
    status: "idle",
    progressText: "",
    stage: null,
    stageProgress: 0,
    error: null,
    highlightIds: new Set<string>(),
    selectedNodeId: null,
  });
  apiMock.mockReset();
}

beforeEach(resetStore);

const completedPayload = {
  id: "j1",
  address: "bc1qtest",
  status: "completed",
  stage: null,
  risk_level: "low",
  subgraph: { nodes: [], edges: [] },
};

const processingPayload = {
  id: "j1",
  address: "bc1qtest",
  status: "processing",
  stage: "building_subgraph",
};

describe("pollJudgment", () => {
  it("首轮轮询即 completed（页面刷新恢复）：stageProgress 直接置满，不重放流势", async () => {
    apiMock.mockResolvedValueOnce(completedPayload);

    await useAnalysisStore.getState().pollJudgment("j1");

    const s = useAnalysisStore.getState();
    expect(s.status).toBe("completed");
    expect(s.stageProgress).toBe(ANALYSIS_STAGES.length);
    expect(s.judgment).toEqual(completedPayload);
  });

  it("观察到过运行态后完成：stageProgress 保持当前值，由进度条逐格推进", async () => {
    vi.useFakeTimers();
    apiMock.mockResolvedValueOnce(processingPayload); // 第 1 次轮询
    apiMock.mockResolvedValueOnce(completedPayload); // 第 2 次轮询（2s 间隔后）

    const polling = useAnalysisStore.getState().pollJudgment("j1");
    await vi.advanceTimersByTimeAsync(START_INTERVAL_MS + 100);
    await polling;

    const s = useAnalysisStore.getState();
    expect(s.status).toBe("completed");
    // 强制置满会一次性全勾——保持 0，让 AnalysisStages 流势逐格走到 4
    expect(s.stageProgress).toBe(0);
  });

  it("failed：error 态 + 错误信息入 store", async () => {
    apiMock.mockResolvedValueOnce({
      id: "j1", address: "bc1qtest", status: "failed",
      stage: "llm_judging", error_code: "LLM_VALIDATION_FAILED",
      error_message: "bad evidence",
    });

    await useAnalysisStore.getState().pollJudgment("j1");

    const s = useAnalysisStore.getState();
    expect(s.status).toBe("failed");
    expect(s.error).toBe("bad evidence");
    expect(s.judgment?.error_code).toBe("LLM_VALIDATION_FAILED");
  });

  it("轮询超时转 failed（POLL_TIMEOUT_MS）", async () => {
    vi.useFakeTimers();
    apiMock.mockResolvedValue(processingPayload);

    const polling = useAnalysisStore.getState().pollJudgment("j1");
    await vi.advanceTimersByTimeAsync(120_000);
    await polling;

    expect(useAnalysisStore.getState().status).toBe("failed");
    expect(useAnalysisStore.getState().error).toBe("分析超时，请重试");
  });
});
