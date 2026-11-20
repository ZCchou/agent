import { describe, expect, it } from "vitest";
import type { ExecutionTaskSnapshot } from "../../services/tauri/executionTasks";
import { modelPhaseTiming, retrievalStage } from "./modelPhaseTiming";

const task: ExecutionTaskSnapshot = {
  id: "t", revision: 1, attempt: 1, status: "running", auto_recoveries: 0,
  created_at: 100, updated_at: 102, stage: "model_output",
};

describe("model progress displayed in the real task card", () => {
  it("measures first output wait from dispatch even after another status poll", () => {
    const current_stage = { status: "running", details: { model_phase: "waiting_first_output",
      model_phase_started_at: 110, model_queue_ms: 25300, model_preflight_ms: 1100 } };
    const before = modelPhaseTiming({ ...task, current_stage }, 117);
    expect(before).toEqual({ phase: "waiting_first_output", phaseSeconds: 7,
      timings: [{ phase: "queued", seconds: 25.3 }, { phase: "preflight", seconds: 1.1 }] });
    expect(modelPhaseTiming({ ...task, updated_at: 120, current_stage }, 121)?.phaseSeconds).toBe(11);
  });
  it("shows a model queue only for the actual model stage", () => {
    const current_stage = { queue_waiting: 1 };
    expect(modelPhaseTiming({ ...task, stage: "retrieval", current_stage })).toBeNull();
    expect(modelPhaseTiming({ ...task, stage: "model_queue", current_stage })?.phase).toBe("queued");
  });
  it("keeps retained main-model output timings out of retrieval and reranking", () => {
    const review = { ...task, stage: "document_review" };
    const last = { model_phase: "generating", model_first_output_ms: 5688, model_role: "main_model" };
    const search = { ...review, current_stage: { details: { ...last, stage: "document_review" } },
      document_progress: { event: "searching" } };
    expect(retrievalStage(search)).toBe("retrieval");
    expect(modelPhaseTiming(search)).toBeNull();
    const rerank = { ...search, current_stage: { details: { ...last, stage: "reranking", model_role: "reranker" } } };
    expect(retrievalStage(rerank)).toBe("reranking");
    expect(modelPhaseTiming(rerank)).toBeNull();
    const returned = { ...review, current_stage: { details: { ...last, stage: "document_review" } },
      document_progress: { event: "search_results" } };
    expect(modelPhaseTiming(returned)).toBeNull();
    const next = { ...review, document_progress: { event: "checking" },
      current_stage: { details: { ...last, stage: "model_output" } } };
    expect(retrievalStage(next)).toBeNull();
    expect(modelPhaseTiming(next)?.phase).toBe("generating");
  });
  it("only labels generation after model output and drops finished stages", () => {
    const details = { model_phase: "generating", model_phase_started_at: 130, model_first_output_ms: 20280 };
    const current_stage = { status: "running", details };
    expect(modelPhaseTiming({ ...task, current_stage }, 135)).toEqual({ phase: "generating",
      phaseSeconds: 5, timings: [{ phase: "waiting_first_output", seconds: 20.3 }] });
    expect(modelPhaseTiming({ ...task, current_stage: { ...current_stage, status: "completed" } })).toBeNull();
    expect(modelPhaseTiming({ ...task, current_stage, status: "failed" })).toBeNull();
  });
});
