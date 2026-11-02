import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { TauriModelRunRepository } from "./TauriModelRunRepository";

describe("TauriModelRunRepository", () => {
  beforeEach(() => { vi.mocked(invoke).mockReset(); vi.mocked(listen).mockReset(); });

  it("forwards an edited user-message identity to the trusted start boundary", async () => {
    vi.mocked(invoke).mockResolvedValue({ status: "accepted" });
    await new TauriModelRunRepository().start({ conversationId: "c1", content: "Edited question",
      providerProfileId: "local", modelId: "text", editMessageId: "user-original" });
    expect(invoke).toHaveBeenCalledWith("start_model_run", { request: expect.objectContaining({
      conversation_id: "c1", content: "Edited question", edit_message_id: "user-original",
    }) });
  });

  it("sends selected historical attachment IDs and ranges without client storage authority", async () => {
    vi.mocked(invoke).mockResolvedValue({ run: { id: "r1" } });
    const reference = { attachment_id: "old-notes", source_start: 10, source_end: 50,
      storage_key: "untrusted-path", sha256: "untrusted-hash" };
    await new TauriModelRunRepository().start({
      conversationId: "c1", content: "Use this section", providerProfileId: "local", modelId: "text",
      attachmentReferences: [reference],
    });
    expect(invoke).toHaveBeenCalledWith("start_model_run", { request: expect.objectContaining({
      attachment_references: [{ attachment_id: "old-notes", source_start: 10, source_end: 50 }],
    }) });
  });

  it("maps a model start to the typed long-running command", async () => {
    vi.mocked(invoke).mockResolvedValue({ run: { id: "r1" } });
    await new TauriModelRunRepository().start({
      conversationId: "c1", content: "hello", providerProfileId: "ollama", modelId: "qwen",
      idempotencyKey: "once",
    });
    expect(invoke).toHaveBeenCalledWith("start_model_run", { request: {
      conversation_id: "c1", kind: null, category: null, chat_group_id: null,
      project_id: null, content: "hello", provider_profile_id: "ollama",
      model_id: "qwen", idempotency_key: "once", attachment_staging_ids: [],
      attachment_rejection_staging_ids: [],
    } });
  });

  it("does not bind events to a React page", async () => {
    const dispose = vi.fn();
    vi.mocked(listen).mockResolvedValue(dispose);
    const listener = vi.fn();
    await new TauriModelRunRepository().subscribe(listener);
    expect(listen).toHaveBeenCalledWith("model-run-event", expect.any(Function));
  });

  it("forwards the fixed selector contract without enabling routing", async () => {
    vi.mocked(invoke).mockResolvedValue({ run: { id: "r1" } });
    await new TauriModelRunRepository().start({
      content: "hello", providerProfileId: "ollama", modelId: "qwen",
      selection: {
        mode: "fixed", provider_profile_id: "ollama", model_id: "qwen", source: "user_once",
      },
    });
    expect(invoke).toHaveBeenCalledWith("start_model_run", { request: expect.objectContaining({
      selection: {
        mode: "fixed", provider_profile_id: "ollama", model_id: "qwen", source: "user_once",
      },
    }) });
  });

  it("uses explicit knowledge mutation commands", async () => {
    vi.mocked(invoke).mockResolvedValue({ knowledge: { id: "k1" } });
    const repository = new TauriModelRunRepository();
    await repository.pinKnowledge("k1", true);
    expect(invoke).toHaveBeenCalledWith("pin_project_knowledge", { knowledgeId: "k1", pinned: true });
  });

  it("keeps context continuation on the same run and sends expanded retry explicitly", async () => {
    vi.mocked(invoke).mockResolvedValue({ run: { id: "r1" } });
    const repository = new TauriModelRunRepository();
    await repository.continueContext("r1");
    expect(invoke).toHaveBeenCalledWith("continue_model_run_context", { runId: "r1" });
    await repository.retry("r1", "expanded_retry");
    expect(invoke).toHaveBeenCalledWith("retry_model_run", {
      runId: "r1", contextStrategy: "expanded_retry", webSearchOverride: null,
    });
    await repository.retry("r1", "default", "off");
    expect(invoke).toHaveBeenCalledWith("retry_model_run", {
      runId: "r1", contextStrategy: "default", webSearchOverride: "off",
    });
  });

  it("binds budget approval to the immutable plan hash and can reload the plan", async () => {
    vi.mocked(invoke).mockResolvedValueOnce({ run: { id: "r1" } }).mockResolvedValueOnce({
      plan: { id: "p1", plan_hash: "hash", steps: [] },
    });
    const repository = new TauriModelRunRepository();
    await repository.resolveBudget("r1", "hash", true);
    expect(invoke).toHaveBeenCalledWith("resolve_model_run_budget", {
      runId: "r1", planHash: "hash", approved: true,
    });
    await repository.getPlan("p1");
    expect(invoke).toHaveBeenCalledWith("get_model_run_plan", { planId: "p1" });
  });
});
