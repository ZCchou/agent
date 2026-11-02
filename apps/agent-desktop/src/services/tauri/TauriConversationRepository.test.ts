import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { TauriConversationRepository } from "./TauriConversationRepository";

const invokeMock = vi.mocked(invoke);

describe("TauriConversationRepository", () => {
  beforeEach(() => invokeMock.mockReset());

  it("lists one conversation track with container filters", async () => {
    invokeMock.mockResolvedValue([]);
    await new TauriConversationRepository().list({ kind: "project_work", projectId: "p1" });
    expect(invokeMock).toHaveBeenCalledWith("list_conversations", {
      kind: "project_work", chatGroupId: null, projectId: "p1",
    });
  });

  it("creates standard chats without project scope", async () => {
    invokeMock.mockResolvedValue({ id: "c1" });
    await new TauriConversationRepository().create({
      kind: "standard_chat", title: "Chat", category: "research",
    });
    expect(invokeMock).toHaveBeenCalledWith("create_conversation", {
      kind: "standard_chat", title: "Chat", category: "research",
      chatGroupId: null, projectId: null,
    });
  });

  it("supports soft deletion without exposing the retired chat pipeline", async () => {
    invokeMock.mockResolvedValue(undefined);
    const repository = new TauriConversationRepository();
    await repository.delete("c1");
    expect(invokeMock).toHaveBeenLastCalledWith("delete_conversation", { conversationId: "c1" });
  });

  it("replaces or resets an automatic conversation coordinator through routing.update", async () => {
    invokeMock.mockResolvedValue({ conversation: { id: "c1" } });
    const repository = new TauriConversationRepository();

    await repository.updateRouting("c1", "automatic", "balanced", "deepseek", "v4-pro");
    expect(invokeMock).toHaveBeenLastCalledWith("update_conversation_routing", { request: {
      conversation_id: "c1", mode: "automatic", preset: "balanced",
      provider_profile_id: "deepseek", model_id: "v4-pro",
    } });

    await repository.updateRouting("c1", "automatic", "balanced", null, null);
    expect(invokeMock).toHaveBeenLastCalledWith("update_conversation_routing", { request: {
      conversation_id: "c1", mode: "automatic", preset: "balanced",
      provider_profile_id: null, model_id: null,
    } });
  });

  it("derives work with explicit source and target", async () => {
    invokeMock.mockResolvedValue({ id: "w1" });
    await new TauriConversationRepository().deriveWork("c1", "p1");
    expect(invokeMock).toHaveBeenCalledWith("derive_work", {
      sourceConversationId: "c1", projectId: "p1", title: null,
    });
  });

  it("uses typed resource-management commands", async () => {
    invokeMock.mockResolvedValue([]);
    const repository = new TauriConversationRepository();
    await repository.reorder({ kind: "standard_chat", containerId: "g1", orderedIds: ["c2", "c1"] });
    expect(invokeMock).toHaveBeenLastCalledWith("reorder_conversations", {
      kind: "standard_chat", containerId: "g1", orderedIds: ["c2", "c1"],
    });
    await repository.moveChat("c1", null, 0);
    expect(invokeMock).toHaveBeenLastCalledWith("move_chat", {
      conversationId: "c1", chatGroupId: null, targetIndex: 0,
    });
    await repository.moveWork("w1", "p2", 1);
    expect(invokeMock).toHaveBeenLastCalledWith("move_work", {
      conversationId: "w1", projectId: "p2", targetIndex: 1,
    });
    await repository.copyWork("w1", "p2");
    expect(invokeMock).toHaveBeenLastCalledWith("copy_work", {
      sourceConversationId: "w1", projectId: "p2", title: null,
    });
  });
});
