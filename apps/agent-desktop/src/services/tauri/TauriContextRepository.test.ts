import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { TauriContextRepository } from "./TauriContextRepository";

it("passes only the selected model identity when explicitly resuming compaction", async () => {
  vi.mocked(invoke).mockResolvedValue({ compaction: null });
  const target = { provider_profile_id: "allowed-local", model_id: "larger-context" };
  await new TauriContextRepository().resumeCompaction("conversation", target);
  expect(invoke).toHaveBeenLastCalledWith("resume_context_compaction", {
    conversationId: "conversation", target,
  });
});

describe("TauriContextRepository", () => {
  beforeEach(() => vi.mocked(invoke).mockReset());

  it("loads snapshots and individual sources through scoped commands", async () => {
    vi.mocked(invoke)
      .mockResolvedValueOnce({ snapshot: { id: "s1" } })
      .mockResolvedValueOnce({ source: { item_id: "i1", source_type: "message", source: {} } })
      .mockResolvedValueOnce({ compaction: null })
      .mockResolvedValueOnce({ compaction: { id: "c1", status: "running" } })
      .mockResolvedValueOnce({ compaction: { id: "c1", status: "interrupted" } });
    const repository = new TauriContextRepository();
    await repository.preview("r1");
    expect(invoke).toHaveBeenCalledWith("get_run_context", { runId: "r1" });
    await repository.source("r1", "i1");
    expect(invoke).toHaveBeenCalledWith("get_context_source", { runId: "r1", itemId: "i1" });
    await repository.compactionStatus("conversation-1");
    expect(invoke).toHaveBeenCalledWith("get_context_compaction_status", { conversationId: "conversation-1" });
    await repository.resumeCompaction("conversation-1");
    expect(invoke).toHaveBeenCalledWith("resume_context_compaction", { conversationId: "conversation-1" });
    await repository.pauseCompaction("conversation-1");
    expect(invoke).toHaveBeenCalledWith("pause_context_compaction", { conversationId: "conversation-1" });
  });
});
