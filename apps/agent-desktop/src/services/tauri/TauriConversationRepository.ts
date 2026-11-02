import { invoke } from "@tauri-apps/api/core";
import type {
  CreateConversationInput,
  ConversationRepository,
  ReorderConversationsInput,
  UpdateConversationInput,
} from "../repositories/ConversationRepository";
import type { Conversation, ConversationFilter } from "../../types/domain";

export class TauriConversationRepository implements ConversationRepository {
  list(filter: ConversationFilter = {}): Promise<Conversation[]> {
    return invoke("list_conversations", {
      kind: filter.kind ?? null,
      chatGroupId: filter.chatGroupId ?? null,
      projectId: filter.projectId ?? null,
    });
  }

  get(conversationId: string): Promise<Conversation> {
    return invoke("get_conversation", { conversationId });
  }

  create(input: CreateConversationInput): Promise<Conversation> {
    return invoke("create_conversation", {
      kind: input.kind,
      title: input.title,
      category: input.category ?? null,
      chatGroupId: input.chatGroupId ?? null,
      projectId: input.projectId ?? null,
    });
  }

  update(conversationId: string, input: UpdateConversationInput): Promise<Conversation> {
    return invoke("update_conversation", {
      conversationId,
      title: input.title,
      category: input.category ?? null,
    });
  }

  deriveWork(sourceConversationId: string, projectId: string, title?: string): Promise<Conversation> {
    return invoke("derive_work", { sourceConversationId, projectId, title: title ?? null });
  }

  reorder(input: ReorderConversationsInput): Promise<Conversation[]> {
    return invoke("reorder_conversations", { kind: input.kind,
      containerId: input.containerId, orderedIds: input.orderedIds });
  }

  moveChat(conversationId: string, chatGroupId: string | null, targetIndex: number): Promise<Conversation> {
    return invoke("move_chat", { conversationId, chatGroupId, targetIndex });
  }

  moveWork(conversationId: string, projectId: string, targetIndex: number): Promise<Conversation> {
    return invoke("move_work", { conversationId, projectId, targetIndex });
  }

  copyWork(sourceConversationId: string, projectId: string, title?: string): Promise<Conversation> {
    return invoke("copy_work", { sourceConversationId, projectId, title: title ?? null });
  }

  delete(conversationId: string): Promise<void> {
    return invoke("delete_conversation", { conversationId });
  }
  updateModel(conversationId: string, providerProfileId: string | null, modelId: string | null): Promise<Conversation> {
    return invoke("update_conversation_model", { conversationId, providerProfileId, modelId });
  }
  updatePurpose(conversationId: string, purposeOverride: import("../../types/domain").ProjectCategory | null): Promise<Conversation> {
    return invoke("update_work_purpose", { conversationId, purposeOverride });
  }
  async updateWebSearch(conversationId: string, mode: "inherit" | "off" | "auto"): Promise<Conversation> {
    return (await invoke<{ conversation: Conversation }>("update_conversation_web_search", { conversationId, mode })).conversation;
  }
  async updateRouting(
    conversationId: string,
    mode: "fixed" | "automatic",
    preset?: "quality" | "balanced" | "economy" | "local_first",
    providerProfileId?: string | null,
    modelId?: string | null,
  ): Promise<Conversation> {
    return (await invoke<{ conversation: Conversation }>("update_conversation_routing", { request: {
      conversation_id: conversationId, mode, preset: preset ?? null,
      provider_profile_id: providerProfileId ?? null, model_id: modelId ?? null,
    } })).conversation;
  }
}
