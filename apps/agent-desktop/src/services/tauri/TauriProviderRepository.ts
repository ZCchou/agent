import { invoke } from "@tauri-apps/api/core";
import type { AppSettings } from "../../types/settings";
import type { ProviderRepository, SaveProviderInput } from "../repositories/ProviderRepository";

export class TauriProviderRepository implements ProviderRepository {
  connect(providerId: string): Promise<void> {
    return invoke("connect_model_provider", { providerId });
  }
  save(provider: SaveProviderInput): Promise<AppSettings> {
    return invoke("save_model_provider", { provider });
  }
  refreshModels(providerId: string): Promise<AppSettings> {
    return invoke("refresh_provider_models", { providerId });
  }
  setEnabled(providerId: string, enabled: boolean, confirmAffected = false): Promise<AppSettings> {
    return invoke("set_model_provider_enabled", { providerId, enabled, confirmAffectedConversations: confirmAffected });
  }
  countReferences(providerId: string): Promise<number> {
    return invoke("count_provider_conversation_references", { providerId });
  }
  delete(providerId: string, confirmAffected = false): Promise<AppSettings> {
    return invoke("delete_model_provider", { providerId, confirmAffectedConversations: confirmAffected });
  }
}
