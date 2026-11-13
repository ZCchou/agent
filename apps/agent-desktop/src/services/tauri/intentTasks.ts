import { invoke } from "@tauri-apps/api/core";
import type { StartedModelRun } from "../repositories/ModelRunRepository";

export interface IntentTask {
  id: string;
  scope: string;
  revision: number;
  status: "queued" | "loading" | "identifying" | "waiting_user" | "completed" | "failed" | "cancelled" | "interrupted";
  request: Record<string, unknown>;
  attempts: number;
  elapsed_seconds: number;
  remaining_seconds: number;
  timeout_seconds: number;
  budget_seconds: number;
  budget_remaining_seconds: number;
  can_extend: boolean;
  can_retry: boolean;
  clarification: null | { summary: string; questions: Array<{ id: string; question: string; options?: string[] }> };
  error: null | { code: string; message: string; details?: unknown };
  result: StartedModelRun | null;
}

type IntentAction = "respond" | "extend" | "retry" | "cancel";
type Waiter = { resolve(value: StartedModelRun): void; reject(error: Error): void };
const active = (task: IntentTask) => ["queued", "loading", "identifying"].includes(task.status);

export class IntentTaskStore {
  private tasks = new Map<string, IntentTask>();
  private listeners = new Set<() => void>();
  private waiters = new Map<string, Waiter[]>();
  private timers = new Map<string, ReturnType<typeof setTimeout>>();
  private actions = new Set<string>();
  private consumed = new Set<string>();
  private cancelledScopes = new Set<string>();
  begin(scope: string) { this.cancelledScopes.delete(scope); }
  async cancelScope(scope: string) {
    this.cancelledScopes.add(scope);
    const task = this.get(scope);
    if (task && !["completed", "cancelled"].includes(task.status)) await this.act(scope, "cancel");
  }
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  get = (scope: string): IntentTask | undefined => this.tasks.get(scope);
  isBusy = (id: string) => this.actions.has(id);
  hasWaiter = (id: string) => this.waiters.has(id);
  consume(id: string) { this.consumed.add(id); }
  isConsumed(id: string) { return this.consumed.has(id); }
  private emit() { this.listeners.forEach((listener) => listener()); }

  update(task: IntentTask) {
    const current = [...this.tasks.values()].find((value) => value.id === task.id);
    if (current && (current.scope !== task.scope || current.revision > task.revision)) return;
    this.tasks.set(task.scope, task);
    const timer = this.timers.get(task.id);
    if (timer) clearTimeout(timer);
    this.timers.delete(task.id);
    const pending = this.waiters.get(task.id);
    if (task.status === "completed" && task.result) {
      if (pending) this.consume(task.id);
      pending?.forEach((waiter) => waiter.resolve(task.result!));
      this.waiters.delete(task.id);
    } else if (task.status === "cancelled" || (task.status === "failed" && !task.can_retry)) {
      pending?.forEach((waiter) => waiter.reject(Object.assign(new Error(task.error ? `${task.error.code}: ${task.error.message}` : "intent_cancelled"), { details: task.error?.details })));
      this.waiters.delete(task.id);
    }
    if (active(task)) this.pollLater(task);
    this.emit();
  }

  wait(task: IntentTask): Promise<StartedModelRun> {
    return new Promise((resolve, reject) => {
      const pending = this.waiters.get(task.id) ?? [];
      pending.push({ resolve, reject });
      this.waiters.set(task.id, pending);
      this.update(task);
      if (this.cancelledScopes.has(task.scope)) void this.act(task.scope, "cancel").catch(reject);
    });
  }

  private pollLater(task: IntentTask) {
    this.timers.set(task.id, setTimeout(() => {
      this.timers.delete(task.id);
      void invoke<{ task: IntentTask }>("intent_request", { action: "status", params: { task_id: task.id } })
        .then(({ task: next }) => this.update(next))
        .catch(() => { if (active(this.tasks.get(task.scope) ?? task)) this.pollLater(task); });
    }, 750));
  }

  async restore(scope: string) {
    const previous = this.get(scope);
    const { tasks } = await invoke<{ tasks: IntentTask[] }>("intent_request", { action: "list", params: { scope } });
    const task = tasks.find((value) => value.scope === scope);
    if (task && this.get(scope) === previous) this.update(task);
  }

  async act(scope: string, action: IntentAction, answers?: Record<string, string>) {
    const task = this.get(scope);
    if (!task || this.actions.has(task.id)) return;
    this.actions.add(task.id); this.emit();
    try {
      const result = await invoke<{ task: IntentTask }>("intent_request", { action,
        params: { task_id: task.id, revision: task.revision, ...(answers ? { answers } : {}) } });
      if (result.task.id !== task.id || result.task.scope !== scope) throw new Error("intent_scope_mismatch");
      this.update(result.task);
    } finally {
      this.actions.delete(task.id); this.emit();
      const current = this.get(scope);
      if (action !== "cancel" && this.cancelledScopes.has(scope) && current?.id === task.id
        && !["completed", "cancelled"].includes(current.status)) await this.act(scope, "cancel");
    }
  }

  dispose() { this.timers.forEach(clearTimeout); this.timers.clear(); this.listeners.clear(); }
}

export const intentTasks = new IntentTaskStore();
