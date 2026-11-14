import { invoke } from "@tauri-apps/api/core";
import type { ExecutionWaitPolicy } from "../../types/settings";

export type ExecutionOwner = { kind: "intent" | "run" | "task"; id: string };
export interface ExecutionTaskSnapshot {
  id: string;
  revision: number;
  attempt: number;
  sequence?: number;
  status: "running" | "waiting_recovery" | "completed" | "failed" | "cancelled" | "interrupted";
  run_id?: string | null;
  intent_task_id?: string | null;
  stage?: string | null;
  auto_recoveries: number;
  policy?: ExecutionWaitPolicy;
  created_at: number;
  updated_at: number;
  elapsed_seconds?: number;
  seconds_since_progress?: number;
  error_code?: string | null;
  can_continue?: boolean;
  can_retry?: boolean;
  can_cancel?: boolean;
  recovery_action?: "run_retry";
  recovery_run_id?: string;
  current_stage?: {
    completed?: number | null; total?: number | null; progress_at?: number;
    details?: Record<string, unknown>; details_json?: string;
  } | null;
}

export function acceptExecutionSnapshot(previous: ExecutionTaskSnapshot | null, next: ExecutionTaskSnapshot,
  owner: ExecutionOwner): boolean {
  if (!next || typeof next.id !== "string" || !Number.isInteger(next.revision) || !Number.isInteger(next.attempt)) return false;
  const ownerId = owner.kind === "task" ? next.id : owner.kind === "run" ? next.run_id : next.intent_task_id;
  if (ownerId !== owner.id) return false;
  if (!previous || previous.id !== next.id) return true;
  if (next.revision !== previous.revision) return next.revision > previous.revision;
  if (next.attempt !== previous.attempt) return next.attempt > previous.attempt;
  return (next.sequence ?? next.updated_at) >= (previous.sequence ?? previous.updated_at);
}

export async function getExecutionTask(owner: ExecutionOwner): Promise<ExecutionTaskSnapshot | null> {
  const result = await invoke<{ task: ExecutionTaskSnapshot | null }>("execution_request", {
    action: "status", params: owner.kind === "task" ? { task_id: owner.id } : { owner_kind: owner.kind, owner_id: owner.id },
  });
  return result?.task ?? null;
}

type ControlAction = "continue" | "retry" | "cancel";
const activeControls = new Map<string, Promise<ExecutionTaskSnapshot>>();

export function controlExecutionTask(task: ExecutionTaskSnapshot, action: ControlAction): Promise<ExecutionTaskSnapshot> {
  const identity = `${task.id}:${task.revision}:${action}`;
  const pending = activeControls.get(identity);
  if (pending) return pending;
  const request = (async () => {
    try {
      const result = await invoke<{ task: ExecutionTaskSnapshot }>("execution_request", {
        action, params: { task_id: task.id, revision: task.revision, idempotency_key: crypto.randomUUID() },
      });
      if (result?.task?.id !== task.id) throw new Error("execution_task_mismatch");
      return result.task;
    } catch (error) {
      const result = await invoke<{ task: ExecutionTaskSnapshot }>("execution_request", {
        action: "status", params: { task_id: task.id },
      }).catch(() => null);
      const current = result?.task;
      const confirmed = current?.id === task.id && (
        (action === "cancel" && current.status === "cancelled")
        || (action !== "cancel" && (current.revision > task.revision || current.attempt > task.attempt))
      );
      if (confirmed && current) return current;
      throw error;
    }
  })();
  activeControls.set(identity, request);
  void request.finally(() => { if (activeControls.get(identity) === request) activeControls.delete(identity); }).catch(() => undefined);
  return request;
}
