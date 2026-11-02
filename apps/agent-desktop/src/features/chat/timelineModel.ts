import type { ChangeSet, ChatMessage, ToolOperation, WebOperation } from "../../types/domain";
import { safeSearchCompleteness } from "./searchPresentation";

export interface ToolEventSummary {
  run_id: string | null;
  operation_id: string | null;
  tool_name: string;
  status: string;
  output_bytes: number;
  error_code: string | null;
  search_completeness: ToolOperation["search_completeness"];
}

export interface ExecutionSummary {
  run_id: string;
  selection_mode?: "fixed" | "automatic";
  coordinator_provider_profile_id: string;
  coordinator_model_id: string;
  selection_reason_code: string;
  intent_resolution?: {
    source?: "rules" | "advisor" | "merged" | "fallback" | string;
    confidence?: "high" | "medium" | "low" | string;
    signals?: string[];
    explanation_code?: string;
    advisor_error_code?: string | null;
    effective_requirements?: Record<string, unknown>;
    relation?: "new" | "continuation" | "follow_up" | string;
    pending_action_id?: string;
    follow_up?: "none" | "confirm" | "reject" | string;
  };
  steps: Array<{
    id?: string;
    step_type: "intent_analysis" | "vision_analysis" | "web_research" | "reasoning_analysis" | "final_response" | "grounding_fallback";
    provider_profile_id: string;
    model_id: string;
    invocation_route_id?: string;
    protocol_id?: string;
    adapter_id?: string;
    status?: "queued" | "running" | "completed" | "skipped" | "failed" | "cancelled" | "interrupted" | "unknown_outcome" | "stale";
  }>;
  invocations?: Array<{
    id: string;
    step_id: string;
    round_no: number;
    attempt_no: number;
    kind: "model" | "host_search";
    provider_or_backend_id: string;
    model_id?: string | null;
    route_id: string;
    protocol_id: string;
    adapter_id: string;
    capability_signature: string[];
    status: string;
    error_code?: string | null;
  }>;
}

export type ConversationTimelineItem =
  | { kind: "message"; key: string; message: ChatMessage }
  | {
    kind: "activity";
    key: string;
    runId: string | null;
    events: Array<{ message: ChatMessage; summary: ToolEventSummary }>;
    changeSets: ChangeSet[];
    operations: ToolOperation[];
    webOperations: WebOperation[];
    executionSummary: ExecutionSummary | null;
    progress: Array<{ message: ChatMessage; summary: PublicProgress }>;
  };

export interface PublicProgress {
  version: 1;
  run_id: string;
  attempt_id: string;
  round_no: number;
  content: string;
}

export function parsePublicProgress(message: ChatMessage): PublicProgress | null {
  if (message.role !== "system_event" || message.event_type !== "assistant_progress") return null;
  try {
    const value = JSON.parse(message.content) as Partial<PublicProgress>;
    if (value.version !== 1 || typeof value.run_id !== "string" || !value.run_id
      || typeof value.attempt_id !== "string" || !value.attempt_id
      || !Number.isInteger(value.round_no) || typeof value.content !== "string") return null;
    return value as PublicProgress;
  } catch { return null; }
}

export function parseToolEvent(message: ChatMessage): ToolEventSummary | null {
  if (message.event_type !== "tool_operation") return null;
  try {
    const value = JSON.parse(message.content) as Partial<ToolEventSummary>;
    if (typeof value.tool_name !== "string" || typeof value.status !== "string") return null;
    return {
      run_id: typeof value.run_id === "string" ? value.run_id : null,
      operation_id: typeof value.operation_id === "string" ? value.operation_id : null,
      tool_name: value.tool_name,
      status: value.status,
      output_bytes: typeof value.output_bytes === "number" ? value.output_bytes : 0,
      error_code: typeof value.error_code === "string" ? value.error_code : null,
      search_completeness: value.tool_name === "fs.search" ? safeSearchCompleteness(value.search_completeness) : null,
    };
  } catch {
    return null;
  }
}

export function parseExecutionSummary(message: ChatMessage): ExecutionSummary | null {
  if (message.event_type !== "execution_summary") return null;
  try {
    const value = JSON.parse(message.content) as Partial<ExecutionSummary>;
    if (typeof value.run_id !== "string" || typeof value.coordinator_model_id !== "string"
      || !Array.isArray(value.steps)) return null;
    return value as ExecutionSummary;
  } catch {
    return null;
  }
}

export function buildConversationTimeline(
  messages: ChatMessage[], changeSets: ChangeSet[], operations: ToolOperation[],
  webOperations: WebOperation[] = [],
): ConversationTimelineItem[] {
  const activityByRun = new Map<string, Extract<ConversationTimelineItem, { kind: "activity" }>>();
  const inserted = new Set<string>();
  const result: ConversationTimelineItem[] = [];

  const activityFor = (runId: string) => {
    let item = activityByRun.get(runId);
    if (!item) {
      item = { kind: "activity", key: `activity-${runId}`, runId, events: [], changeSets: [], operations: [], webOperations: [], executionSummary: null, progress: [] };
      activityByRun.set(runId, item);
    }
    return item;
  };
  for (const changeSet of changeSets) activityFor(changeSet.run_id).changeSets.push(changeSet);
  for (const operation of operations) activityFor(operation.run_id).operations.push(operation);
  for (const operation of webOperations) activityFor(operation.run_id).webOperations.push(operation);
  for (const activity of activityByRun.values()) {
    activity.changeSets.sort((left, right) => (left.created_at ?? "").localeCompare(right.created_at ?? ""));
    activity.operations.sort((left, right) => left.sequence - right.sequence);
    activity.webOperations.sort((left, right) => left.sequence - right.sequence);
  }

  let legacyIndex = 0;
  let activeLegacy: Extract<ConversationTimelineItem, { kind: "activity" }> | null = null;
  for (const message of messages) {
    const progress = parsePublicProgress(message);
    if (progress) {
      activeLegacy = null;
      const activity = activityFor(progress.run_id);
      activity.progress.push({ message, summary: progress });
      if (!inserted.has(activity.key)) {
        inserted.add(activity.key);
        result.push(activity);
      }
      continue;
    }
    const executionSummary = parseExecutionSummary(message);
    if (executionSummary) {
      activeLegacy = null;
      const activity = activityFor(executionSummary.run_id);
      activity.executionSummary = executionSummary;
      if (!inserted.has(activity.key)) {
        inserted.add(activity.key);
        result.push(activity);
      }
      continue;
    }
    const summary = parseToolEvent(message);
    if (!summary) {
      activeLegacy = null;
      result.push({ kind: "message", key: message.id, message });
      for (const operation of webOperations) {
        if (operation.user_message_id !== message.id) continue;
        const activity = activityFor(operation.run_id);
        if (!inserted.has(activity.key)) {
          inserted.add(activity.key);
          result.push(activity);
        }
      }
      continue;
    }
    if (summary.run_id) {
      activeLegacy = null;
      const activity = activityFor(summary.run_id);
      activity.events.push({ message, summary });
      if (!inserted.has(activity.key)) {
        inserted.add(activity.key);
        result.push(activity);
      }
      continue;
    }
    if (!activeLegacy) {
      legacyIndex += 1;
      activeLegacy = {
        kind: "activity", key: `activity-legacy-${legacyIndex}-${message.id}`,
        runId: null, events: [], changeSets: [], operations: [], webOperations: [], executionSummary: null, progress: [],
      };
      result.push(activeLegacy);
    }
    activeLegacy.events.push({ message, summary });
  }

  for (const activity of activityByRun.values()) {
    if (!inserted.has(activity.key)) result.push(activity);
  }
  return result;
}
