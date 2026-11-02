import type { SearchCompleteness } from "../../types/domain";

const STOP_REASONS = new Set(["files", "entries", "depth", "bytes", "deadline", "cancelled", "authorization_changed"]);

export type SearchPresentation =
  | { state: "complete"; reason: null; inspectedFiles: number }
  | { state: "incomplete"; reason: NonNullable<SearchCompleteness["stop_reason"]>; inspectedFiles: number }
  | { state: "unknown"; reason: null; inspectedFiles: null };

// Treat older and malformed audit records as unknown. Never infer a complete scan from tool success.
export function searchPresentation(value: unknown): SearchPresentation {
  if (!value || typeof value !== "object" || Array.isArray(value)) return { state: "unknown", reason: null, inspectedFiles: null };
  const scan = value as Record<string, unknown>;
  if (typeof scan.complete !== "boolean" || typeof scan.truncated !== "boolean"
    || scan.complete === scan.truncated || !Number.isInteger(scan.inspected_files)
    || (scan.inspected_files as number) < 0 || (scan.inspected_files as number) > 10000) {
    return { state: "unknown", reason: null, inspectedFiles: null };
  }
  if (scan.complete && scan.stop_reason === null) {
    return { state: "complete", reason: null, inspectedFiles: scan.inspected_files as number };
  }
  if (scan.truncated && typeof scan.stop_reason === "string" && STOP_REASONS.has(scan.stop_reason)) {
    return { state: "incomplete", reason: scan.stop_reason as NonNullable<SearchCompleteness["stop_reason"]>, inspectedFiles: scan.inspected_files as number };
  }
  return { state: "unknown", reason: null, inspectedFiles: null };
}

export function safeSearchCompleteness(value: unknown): SearchCompleteness | null {
  const display = searchPresentation(value);
  return display.state === "unknown" ? null : {
    complete: display.state === "complete", truncated: display.state === "incomplete",
    stop_reason: display.reason, inspected_files: display.inspectedFiles,
  };
}
