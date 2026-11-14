import type { ExecutionWaitPolicy } from "../../types/settings";

export function defaultExecutionWaitPolicy(): ExecutionWaitPolicy {
  return {
    mode: "adaptive", long_running_notice_seconds: 90, no_progress_probe_seconds: 300,
    max_total_seconds: null, max_auto_recoveries: 2,
  };
}

export function executionWaitNumber(input: string, minimum: number, maximum: number): number | null {
  if (!input.trim()) return null;
  const value = Number(input);
  return Number.isInteger(value) && value >= minimum && value <= maximum ? value : null;
}
