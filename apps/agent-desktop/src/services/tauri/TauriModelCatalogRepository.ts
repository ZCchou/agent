import { invoke } from "@tauri-apps/api/core";
import type { AppSettings, RouteHealthKey, RouteHealthRecord } from "../../types/settings";
import type { ModelCatalogRepository, SettingsApplyResult } from "../repositories/ModelCatalogRepository";

export class TauriModelCatalogRepository implements ModelCatalogRepository {
  setEnabled(providerId: string, modelId: string, enabled: boolean, confirmAffected = false): Promise<AppSettings> {
    return invoke("set_provider_model_enabled", { change: {
      provider_id: providerId, model_id: modelId, enabled,
      confirm_affected_conversations: confirmAffected,
    } });
  }
  setAutomaticRouting(providerId: string, modelId: string, allowed: boolean, confirmAffected = false): Promise<AppSettings> {
    return invoke("set_model_automatic_routing", { change: {
      provider_id: providerId, model_id: modelId, allowed,
      confirm_affected_conversations: confirmAffected,
    } });
  }
  countReferences(providerId: string, modelId: string): Promise<number> {
    return invoke("count_conversation_model_references", { providerId, modelId });
  }
  addManual(providerId: string, modelId: string): Promise<AppSettings> {
    return invoke("add_manual_provider_model", { providerId, modelId });
  }
  removeManual(providerId: string, modelId: string, confirmAffectedConversations = false): Promise<AppSettings> {
    return invoke("remove_manual_provider_model", { providerId, modelId, confirmAffectedConversations });
  }
  updateDefault(providerId: string, modelId: string): Promise<SettingsApplyResult> {
    return invoke("update_default_model", { providerProfileId: providerId, modelId });
  }
  probeTools(providerId: string, modelId: string): Promise<AppSettings> {
    return invoke("probe_model_tool_capability", { providerId, modelId });
  }
  updateContextLimits(providerId: string, modelId: string, contextLength: number | null, maxOutputTokens: number | null): Promise<AppSettings> {
    return invoke("update_model_context_limits", { request: {
      provider_id: providerId, model_id: modelId,
      context_length: contextLength, max_output_tokens: maxOutputTokens,
    } });
  }
  updateRoutingMetadata(providerId: string, modelId: string, inputPrice: string | null,
    outputPrice: string | null, qualityScore: number | null, priority: number): Promise<AppSettings> {
    return invoke("update_model_routing_metadata", { request: {
      provider_id: providerId, model_id: modelId, input_price: inputPrice,
      output_price: outputPrice, quality_score: qualityScore, priority,
    } });
  }
  listRouteHealth(): Promise<RouteHealthRecord[]> {
    return invoke("list_route_health");
  }
  resetRouteHealth(key: RouteHealthKey): Promise<RouteHealthRecord[]> {
    return invoke("reset_route_health", { key });
  }
}
