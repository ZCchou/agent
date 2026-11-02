import type { AppSettings, ProviderKind, ProviderPresetId } from "../../types/settings";

export interface SaveProviderInput {
  id: string;
  name: string;
  kind: ProviderKind;
  preset_id: ProviderPresetId;
  entered_base_url: string;
  enabled: boolean;
  api_key?: string;
  primary_protocol?: "openai_chat" | "openai_responses" | "anthropic_messages";
}

export interface ProviderRepository {
  connect(providerId: string): Promise<void>;
  save(provider: SaveProviderInput): Promise<AppSettings>;
  refreshModels(providerId: string): Promise<AppSettings>;
  setEnabled(providerId: string, enabled: boolean, confirmAffected?: boolean): Promise<AppSettings>;
  countReferences(providerId: string): Promise<number>;
  delete(providerId: string, confirmAffected?: boolean): Promise<AppSettings>;
}
