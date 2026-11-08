import { invoke } from "@tauri-apps/api/core";
import type { WebSearchRepository, SearchBackendInput } from "../repositories/WebSearchRepository";
import type { AppSettings, WebSearchSettings } from "../../types/settings";
import type { Conversation, Project, WebOperation, WebSource } from "../../types/domain";
import { enqueueSettingsWrite, invokeSettingsMutation, patchWebSearch } from "./settingsPersistence";

export class TauriWebSearchRepository implements WebSearchRepository {
  updateSettings(settings: Partial<WebSearchSettings>): Promise<AppSettings> {
    return patchWebSearch(settings);
  }
  saveBackend(input: SearchBackendInput): Promise<AppSettings> {
    return enqueueSettingsWrite(() => invokeSettingsMutation("save_search_backend", { backend: {
      id: input.id, name: input.name, kind: input.kind, base_url: input.baseUrl,
      enabled: input.enabled, api_key: input.apiKey ?? null,
    } }));
  }
  deleteBackend(backendId: string): Promise<AppSettings> {
    return enqueueSettingsWrite(() => invokeSettingsMutation("delete_search_backend", { backendId }));
  }
  testBackend(backendId: string): Promise<{ available: boolean; message: string; tested_at: string }> {
    return enqueueSettingsWrite(() => invoke("test_search_backend", { backendId }));
  }
  async updateConversation(conversationId: string, mode: "inherit" | "off" | "auto"): Promise<Conversation> {
    return (await invoke<{ conversation: Conversation }>("update_conversation_web_search", { conversationId, mode })).conversation;
  }
  async updateProject(projectId: string, policy: "disabled" | "allow_search"): Promise<Project> {
    return (await invoke<{ project: Project }>("update_project_web_access", { projectId, policy })).project;
  }
  async listOperations(runId: string): Promise<WebOperation[]> {
    return (await invoke<{ operations: WebOperation[] }>("list_web_operations", { runId })).operations;
  }
  async listConversationOperations(conversationId: string): Promise<WebOperation[]> {
    return (await invoke<{ operations: WebOperation[] }>("list_conversation_web_operations", { conversationId })).operations;
  }
  async previewSource(runId: string, sourceId: string): Promise<WebSource> {
    return (await invoke<{ source: WebSource }>("preview_web_source", { runId, sourceId })).source;
  }
  probeNative(providerId: string, modelId: string): Promise<AppSettings> {
    return invokeSettingsMutation("probe_native_web_search", { providerId, modelId });
  }
}
