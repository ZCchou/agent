import type { AppSettings, SearchBackendSettings, WebSearchSettings } from "../../types/settings";
import type { Conversation, Project, WebOperation, WebSource } from "../../types/domain";

export interface SearchBackendInput {
  id: string;
  name: string;
  kind: "tavily" | "searxng";
  baseUrl: string;
  enabled: boolean;
  apiKey?: string;
}

export interface WebSearchRepository {
  updateSettings(settings: Partial<WebSearchSettings>): Promise<AppSettings>;
  saveBackend(input: SearchBackendInput): Promise<AppSettings>;
  deleteBackend(id: string): Promise<AppSettings>;
  testBackend(id: string): Promise<{ available: boolean; message: string; tested_at: string }>;
  updateConversation(id: string, mode: "inherit" | "off" | "auto"): Promise<Conversation>;
  updateProject(id: string, policy: "disabled" | "allow_search"): Promise<Project>;
  listOperations(runId: string): Promise<WebOperation[]>;
  listConversationOperations(conversationId: string): Promise<WebOperation[]>;
  previewSource(runId: string, sourceId: string): Promise<WebSource>;
  probeNative(providerId: string, modelId: string): Promise<AppSettings>;
}

export type { SearchBackendSettings };
