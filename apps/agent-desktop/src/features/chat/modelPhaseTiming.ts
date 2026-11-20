import type { ExecutionTaskSnapshot } from "../../services/tauri/executionTasks";

const phases = ["queued", "preflight", "waiting_first_output", "generating"] as const;

const auxiliaryStages = new Set(["integrity_verification", "worker_starting", "tokenization",
  "model_loading", "model_ready", "embedding", "reranking"]);

export function retrievalStage(task: ExecutionTaskSnapshot) {
  const details = task.current_stage?.details ?? {};
  const current = details.stage ?? task.stage;
  const role = details.model_role;
  const auxiliary = role === "embedding" || role === "reranker";
  const event = task.document_progress?.event;
  if (current === "rerank_degraded" || current === "retrieval_cache_hit") return current;
  if (current === "retrieval_scope" || current === "rag_retrieval" || current === "retrieval") return "retrieval";
  if (current === "rag_embed" || current === "rag_rerank") return current;
  if (auxiliary && typeof current === "string") {
    if (current === "result_validation") return "validation";
    if (auxiliaryStages.has(current)) return current;
  }
  if (event === "searching" && current !== "model_output") return "retrieval";
  return null;
}

export function modelPhaseTiming(task: ExecutionTaskSnapshot, nowSeconds = Date.now() / 1000) {
  const stage = task.current_stage;
  if (task.status !== "running" || !stage || (stage.status && stage.status !== "running")) return null;
  const details = stage.details ?? {};
  if (retrievalStage(task) || details.model_role === "reranker" || details.model_role === "embedding") return null;
  // A single document-review stage hosts several child invocations. After it
  // resumes business work, its merged details can still contain the last call.
  if (details.model_phase === "generating" && task.stage === "document_review" && details.stage !== "model_output") return null;
  const reportedPhase = phases.find(phase => phase === details.model_phase);
  const phase = stage.queue_waiting && (task.stage === "model_queue" || reportedPhase) ? "queued" : reportedPhase;
  if (!phase) return null;
  const phaseStartedAt = details.model_phase_started_at;
  const phaseSeconds = phase === details.model_phase && typeof phaseStartedAt === "number" && Number.isFinite(phaseStartedAt)
    ? Math.max(0, Math.floor(nowSeconds - phaseStartedAt)) : null;
  const timings = ([
    ["queued", details.model_queue_ms],
    ["preflight", details.model_preflight_ms],
    ["waiting_first_output", details.model_first_output_ms],
  ] as const).flatMap(([phase, value]) =>
    typeof value === "number" && Number.isFinite(value) && value >= 0
      ? [{phase, seconds: Math.round(value / 100) / 10}] : []);
  return { phase, phaseSeconds, timings };
}
