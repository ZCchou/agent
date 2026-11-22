const legacyToolLimits = new Map<string, string>([
  ["The run exceeded its tool round limit.", "tool_call_limit_exceeded"],
  ["The run exceeded its read/search limit.", "tool_read_limit_exceeded"],
  ["The run exceeded its file mutation limit.", "tool_mutation_limit_exceeded"],
  ["Tool result exceeds the run data budget.", "tool_data_limit_exceeded"],
  ["A change set exceeds the 20 MiB content budget.", "tool_data_limit_exceeded"],
  ["A text mutation exceeds 10 MiB.", "tool_data_limit_exceeded"],
  ["A text patch exceeds 10 MiB.", "tool_data_limit_exceeded"],
  ["Terminal audit exceeds its limit.", "tool_data_limit_exceeded"],
]);

/** Read old persisted generic failures without guessing from arbitrary error text. */
export function runErrorTranslationKey(code: string, message: string | null): string {
  const specific = code === "tool_limit_exceeded" ? legacyToolLimits.get(message ?? "") : undefined;
  return `errors.${specific ?? code}`;
}
import type { ModelRun, ModelRunEvent } from "../../types/domain";


/** Terminal events carry a result even when the persistence refresh fails. */
export function preservePublicContent(existing: string, incoming: string): string {
  if (!incoming || existing.includes(incoming)) return existing;
  if (!existing || incoming.includes(existing)) return incoming;
  return existing + "\n\n" + incoming;
}

export function applyRunFailure(run: ModelRun, event: ModelRunEvent, streamed = ""): ModelRun {
  if (run.id !== event.run_id) return run;
  const content = preservePublicContent(preservePublicContent(streamed, run.draft?.content ?? ""),
    typeof event.data.fallback_content === "string" ? event.data.fallback_content : "");
  return {
    ...run,
    status: event.event_type === "run.cancelled" ? "cancelled"
      : event.event_type === "run.interrupted" ? "interrupted" : "failed",
    error_code: typeof event.data.error_code === "string" ? event.data.error_code : run.error_code,
    error_message: typeof event.data.message === "string" ? event.data.message : run.error_message,
    draft: content ? { ...run.draft, content, updated_at: event.timestamp } : run.draft,
  };
}
