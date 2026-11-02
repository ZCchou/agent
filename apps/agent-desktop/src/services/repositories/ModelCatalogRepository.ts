import type { AppSettings, RouteHealthKey, RouteHealthRecord } from "../../types/settings";

export interface SettingsApplyResult {
  settings: AppSettings;
  core_sync: "applied" | "pending";
  warning_code?: "core_offline" | "core_sync_failed" | null;
}

export interface ModelCatalogRepository {
  setEnabled(providerId: string, modelId: string, enabled: boolean, confirmAffected?: boolean): Promise<AppSettings>;
  setAutomaticRouting(providerId: string, modelId: string, allowed: boolean, confirmAffected?: boolean): Promise<AppSettings>;
  countReferences(providerId: string, modelId: string): Promise<number>;
  addManual(providerId: string, modelId: string): Promise<AppSettings>;
  removeManual(providerId: string, modelId: string, confirmAffectedConversations?: boolean): Promise<AppSettings>;
  updateDefault(providerId: string, modelId: string): Promise<SettingsApplyResult>;
  probeTools(providerId: string, modelId: string): Promise<AppSettings>;
  updateContextLimits(providerId: string, modelId: string, contextLength: number | null, maxOutputTokens: number | null): Promise<AppSettings>;
  updateRoutingMetadata(providerId: string, modelId: string, inputPrice: string | null,
    outputPrice: string | null, qualityScore: number | null, priority: number): Promise<AppSettings>;
  listRouteHealth(): Promise<RouteHealthRecord[]>;
  resetRouteHealth(key: RouteHealthKey): Promise<RouteHealthRecord[]>;
}
