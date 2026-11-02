import { describe, expect, it } from "vitest";
import type { ChangeSet, ChatMessage, WebOperation } from "../../types/domain";
import { buildConversationTimeline, parseToolEvent } from "./timelineModel";

function message(id: string, role: ChatMessage["role"], content: string, eventType: string | null = null): ChatMessage {
  return { id, conversation_id: "work", role, content, event_type: eventType, source_message_id: null, created_at: id };
}

function changeSet(runId: string): ChangeSet {
  return {
    id: `change-${runId}`, operation_id: `op-${runId}`, run_id: runId, conversation_id: "work", project_id: "project",
    title: "Create file", risk: "routine", approval_mode: "human", mutations: [{ kind: "create_text", path: "a.txt" }],
    status: "completed", expires_at_epoch_ms: 0, recovery_point_id: "recovery", undo_state: "available",
  };
}

describe("buildConversationTimeline", () => {
  it("groups typed public progress with its run before the final answer", () => {
    const progress = message("2", "system_event", JSON.stringify({ version: 1,
      run_id: "run-1", attempt_id: "attempt-1", round_no: 1, content: "Inspecting the project." }), "assistant_progress");
    const items = buildConversationTimeline([message("1", "user", "do it"), progress, message("3", "assistant", "Done.")], [], []);
    expect(items.map((item) => item.kind)).toEqual(["message", "activity", "message"]);
    expect(items[1]).toMatchObject({ runId: "run-1", progress: [{ message: progress,
      summary: { content: "Inspecting the project.", round_no: 1 } }] });
    expect(items[2]).toMatchObject({ message: { content: "Done." } });
  });
  it("retains safe search completeness in standalone legacy events", () => {
    const event = message("search", "system_event", JSON.stringify({
      tool_name: "fs.search", status: "completed", output_bytes: 42,
      search_completeness: { complete: false, truncated: true, stop_reason: "files", inspected_files: 10000,
        matches: ["private-file"], next_cursor: "private-cursor" },
    }), "tool_operation");
    const summary = parseToolEvent(event);
    expect(summary?.search_completeness).toEqual({
      complete: false, truncated: true, stop_reason: "files", inspected_files: 10000,
    });
    expect(buildConversationTimeline([event], [], [])[0]).toMatchObject({
      kind: "activity", events: [{ summary: { search_completeness: summary?.search_completeness } }],
    });
  });
  it("places one run activity at the tool event position without duplicating raw system messages", () => {
    const messages = [
      message("1", "user", "do it"),
      message("2", "system_event", JSON.stringify({ run_id: "run-1", operation_id: "op", tool_name: "fs.propose_changes", status: "completed", output_bytes: 10 }), "tool_operation"),
      message("3", "assistant", "done"),
    ];
    const items = buildConversationTimeline(messages, [changeSet("run-1")], []);
    expect(items.map((item) => item.kind)).toEqual(["message", "activity", "message"]);
    const activity = items[1];
    expect(activity.kind === "activity" && activity.changeSets).toHaveLength(1);
    expect(activity.kind === "activity" && activity.events).toHaveLength(1);
  });

  it("groups only consecutive legacy events and never crosses a user message", () => {
    const legacy = (id: string) => message(id, "system_event", JSON.stringify({ tool_name: "fs.read", status: "completed", output_bytes: 1 }), "tool_operation");
    const items = buildConversationTimeline([legacy("1"), legacy("2"), message("3", "user", "next"), legacy("4")], [], []);
    expect(items.map((item) => item.kind)).toEqual(["activity", "message", "activity"]);
    expect(items[0].kind === "activity" && items[0].events).toHaveLength(2);
    expect(items[2].kind === "activity" && items[2].events).toHaveLength(1);
  });

  it("anchors persisted web-search activity directly after its user message", () => {
    const messages = [message("user-1", "user", "latest news"), message("assistant-1", "assistant", "answer")];
    const operation: WebOperation = {
      id: "web-1", run_id: "run-web", conversation_id: "work", user_message_id: "user-1",
      sequence: 1, route: "host_presearch", backend_id: "search", query_text: "latest news",
      status: "completed", result_count: 1, evidence_status: "traceable_sources",
      verified_source_count: 1, usage: {}, error_code: null,
      sources: [{
        id: "source-1", operation_id: "web-1", title: "Source",
        url: "https://example.com", domain: "example.com", snippet: null,
        published_at: null, retrieved_at: "now", content_hash: null,
        trust: "external_untrusted",
      }],
    };

    const items = buildConversationTimeline(messages, [], [], [operation]);

    expect(items.map((item) => item.kind)).toEqual(["message", "activity", "message"]);
    expect(items[1].kind === "activity" && items[1].webOperations[0].id).toBe("web-1");
  });

  it("preserves immutable invocation summaries for actual-route diagnostics", () => {
    const summary = message("summary", "system_event", JSON.stringify({
      run_id: "run-route",
      selection_mode: "fixed",
      coordinator_provider_profile_id: "deepseek",
      coordinator_model_id: "deepseek-chat",
      selection_reason_code: "fixed_model",
      steps: [],
      invocations: [{
        id: "spec-1", step_id: "step-1", round_no: 1, attempt_no: 1,
        kind: "model", provider_or_backend_id: "deepseek", model_id: "deepseek-chat",
        route_id: "deepseek.anthropic_web", protocol_id: "anthropic_messages",
        adapter_id: "deepseek.anthropic_web", capability_signature: ["text", "native_web_search"],
        status: "completed", error_code: null,
      }],
    }), "execution_summary");

    const items = buildConversationTimeline([summary], [], []);
    expect(items[0].kind === "activity" && items[0].executionSummary?.invocations?.[0]).toMatchObject({
      adapter_id: "deepseek.anthropic_web", protocol_id: "anthropic_messages",
    });
  });

  it("keeps intent resolution metadata visible with the execution summary", () => {
    const summary = message("intent-summary", "system_event", JSON.stringify({
      run_id: "run-intent",
      coordinator_provider_profile_id: "kimi",
      coordinator_model_id: "kimi-k3",
      selection_reason_code: "automatic_capability_match",
      intent_resolution: {
        source: "merged", confidence: "medium", signals: ["file_write"],
        effective_requirements: { files: "write", tools: "required" },
      },
      steps: [],
    }), "execution_summary");

    const items = buildConversationTimeline([summary], [], []);
    expect(items[0].kind === "activity" && items[0].executionSummary?.intent_resolution).toMatchObject({
      source: "merged", confidence: "medium", signals: ["file_write"],
    });
  });
});
