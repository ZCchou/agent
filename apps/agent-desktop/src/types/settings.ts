export type InterfaceLanguage =
  | "system"
  | "en"
  | "zh-CN";


export interface GeneralSettings {
  interface_language:
    InterfaceLanguage;
}

export type RemoteImagePolicy = "ask" | "allow";

export interface ContentPreferences {
  remote_image_policy: RemoteImagePolicy;
}


export interface AppSettings {
  schema_version: number;

  general:
    GeneralSettings;
  sidebar: SidebarPreferences;
  models: ModelSettings;
  content: ContentPreferences;
  web_search: WebSearchSettings;
  routing?: RoutingPreferences;
}

export type ProviderKind = "fake" | "ollama" | "openai_compatible";
export type ProviderPresetId = "builtin_demo" | "ollama" | "deepseek" | "openai" | "moonshot_cn" | "kimi_global" | "custom";
export type EndpointMode = "preset" | "exact" | "legacy_auto_v1";
export interface ModelCapabilities {
  text: "yes" | "no" | "unknown";
  vision: "yes" | "no" | "unknown";
  vision_source: "provider" | "official_manifest" | "user_confirmed" | "runtime" | "unknown";
  vision_status: "declared" | "verified" | "unsupported" | "unknown";
  vision_error_code: string | null;
  vision_verified_at: string | null;
  reasoning: "yes" | "no" | "unknown";
  tools: "yes" | "no" | "unknown";
  context_length: number | null;
  max_output_tokens: number | null;
  context_length_source: "discovered" | "manual" | "fallback";
  input_price_per_million: string | null;
  output_price_per_million: string | null;
  routing_quality_score?: number | null;
  routing_priority?: number;
}
export interface ProviderModelSettings {
  id: string;
  source: "discovered" | "manual";
  status: "available" | "unavailable" | "unverified";
  enabled: boolean;
  allow_automatic_routing: boolean;
  tool_mode: "native" | "structured" | "text_only" | "unknown";
  capabilities: ModelCapabilities;
  capability_states?: ModelCapabilityState[];
  last_seen_at: string | null;
  native_web_search_status?: "declared" | "verified" | "unsupported" | "unknown" | "not_applicable";
  native_web_search_error_code?: string | null;
  native_web_search_verified_at?: string | null;
  route_capabilities?: ModelRouteCapabilityState[];
  tool_contracts?: ModelToolContractState[];
}
export type ToolTransport = "native_function_call" | "structured_json" | "none";
export interface ModelToolContractState {
  route_id: string;
  transport: ToolTransport;
  contract_version: string;
  schema_hash: string;
  status: RouteCapabilityStatus;
  verified_tool_names: string[];
  verified_at?: string | null;
  last_error_code?: string | null;
}
export type NativeSearchAdapterId = "none" | "openai_responses_web_search" | "kimi_builtin_web_search" | "anthropic_messages_web_search";
export interface NativeSearchBinding {
  adapter_id: NativeSearchAdapterId;
  protocol: "none" | "openai_responses" | "openai_chat" | "anthropic_messages";
  endpoint_base_url: string | null;
  source: "official_preset" | "manual";
  adapter_version: string;
}
export interface ProviderProfileSettings {
  id: string;
  name: string;
  kind: ProviderKind;
  preset_id: ProviderPresetId;
  entered_base_url: string;
  resolved_api_base_url: string | null;
  endpoint_mode: EndpointMode;
  enabled: boolean;
  credential_ref: string | null;
  models: ProviderModelSettings[];
  last_discovered_at: string | null;
  primary_protocol?: "memory" | "ollama" | "openai_responses" | "openai_chat" | "anthropic_messages";
  native_search?: NativeSearchBinding;
  invocation_routes?: InvocationRouteBinding[];
}

export type InvocationProtocol = "memory" | "ollama" | "openai_responses" | "openai_chat" | "anthropic_messages";
export type RouteCapabilityStatus = "declared" | "verified" | "unsupported" | "unknown" | "temporary_error" | "configuration_blocked";
export interface ModelCapabilityState {
  capability_id: string;
  declared_status: RouteCapabilityStatus;
  verified_status: RouteCapabilityStatus;
  limits?: Record<string, string>;
  last_verified_at?: string | null;
  error_code?: string | null;
}
export interface InvocationRouteBinding {
  route_id: string;
  route_role: "primary" | "vision" | "native_search";
  protocol: InvocationProtocol;
  adapter_id: string;
  endpoint_base_url: string | null;
  source: "official_preset" | "manual";
  manifest_version: string;
  config_fingerprint: string;
  capabilities: Array<"text" | "vision" | "function_tools" | "native_web_search" | "reasoning" | "text_generation" | "vision_input" | "function_calling" | "web_search" | "structured_web_evidence" | "file_read" | "file_write">;
  source_provenance: "structured" | "opaque" | "none" | "unknown";
  tool_transports?: ToolTransport[];
  preferred_tool_transport?: ToolTransport | null;
}
export interface ModelRouteCapabilityState {
  route_id: string;
  capability_id?: string;
  capability_signature: string[];
  support_status: RouteCapabilityStatus;
  unsupported_reason_code: string | null;
  verified_at: string | null;
  route_config_fingerprint: string;
  evidence_kind?: "structured" | "opaque" | "none" | "unknown";
  health_status?: "ready" | "degraded" | "cooling_down" | "configuration_blocked";
  last_error_code?: string | null;
}

export type RouteHealthStatus = "ready" | "degraded" | "cooling_down" | "configuration_blocked";
export interface RouteHealthKey {
  target_kind: "model" | "host_search";
  provider_or_backend_id: string;
  model_id?: string | null;
  route_id: string;
  capability_signature: string[];
  route_fingerprint: string;
  tool_transport?: ToolTransport;
  tool_contract_version?: string;
  tool_schema_hash?: string;
}
export interface RouteHealthRecord {
  target_kind: "model" | "host_search";
  provider_or_backend_id: string;
  model_id?: string | null;
  route_id: string;
  capability_signature: string[];
  route_fingerprint: string;
  tool_transport?: ToolTransport;
  tool_contract_version?: string;
  tool_schema_hash?: string;
  health_status: RouteHealthStatus;
  consecutive_failures: number;
  last_error_code?: string | null;
  last_failure_at?: string | null;
  retry_at?: string | null;
  blocked_config_generation?: number | null;
}

export function routeHealthKey(record: RouteHealthRecord): RouteHealthKey {
  return {
    target_kind: record.target_kind,
    provider_or_backend_id: record.provider_or_backend_id,
    model_id: record.model_id,
    route_id: record.route_id,
    capability_signature: record.capability_signature,
    route_fingerprint: record.route_fingerprint,
  };
}

export function nativeSearchRoute(provider: ProviderProfileSettings): InvocationRouteBinding | undefined {
  const configured = provider.invocation_routes?.find((route) => route.capabilities.includes("native_web_search"));
  if (configured) return configured;
  const legacy = provider.native_search;
  if (!legacy || legacy.adapter_id === "none" || legacy.protocol === "none") return undefined;
  return {
    route_id: "legacy.native_search", route_role: "native_search",
    protocol: legacy.protocol, adapter_id: legacy.adapter_id,
    endpoint_base_url: legacy.endpoint_base_url, source: legacy.source,
    manifest_version: legacy.adapter_version, config_fingerprint: "legacy",
    capabilities: ["text", "native_web_search"],
    source_provenance: "unknown",
  };
}

export function nativeSearchState(
  provider: ProviderProfileSettings,
  model: ProviderModelSettings,
): ModelRouteCapabilityState | undefined {
  const route = nativeSearchRoute(provider);
  return route
    ? model.route_capabilities?.find((state) => state.route_id === route.route_id
      && state.capability_signature.includes("native_web_search"))
    : undefined;
}

export interface SearchBackendSettings {
  id: string;
  name: string;
  kind: "tavily" | "searxng";
  base_url: string;
  enabled: boolean;
  credential_ref: string | null;
  last_status: string | null;
  last_tested_at: string | null;
}

export interface WebSearchSettings {
  enabled: boolean;
  privacy_acknowledged: boolean;
  default_backend_id: string | null;
  safe_search: boolean;
  default_language: string;
  default_region: string;
  backends: SearchBackendSettings[];
}
export interface RoutingPreferences {
  new_conversation_mode: "automatic";
  default_preset: "quality" | "balanced" | "economy" | "local_first";
  max_model_calls_per_message: number;
  max_specialist_steps: number;
  max_extra_online_cost_usd: string;
  allow_unknown_price_automatic: boolean;
}
export interface ModelSettings {
  providers: ProviderProfileSettings[];
  default_provider_profile_id: string;
  default_model_id: string;
  connect_timeout_secs: number;
  first_output_timeout_secs: number;
  chunk_timeout_secs: number;
  total_timeout_secs: number;
}

export interface SidebarPreferences {
  standard_chats_collapsed: boolean;
  projects_collapsed: boolean;
  collapsed_chat_group_ids: string[];
  collapsed_project_ids: string[];
  width_px: number;
}
