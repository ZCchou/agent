use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

/// 当前 agent 设置文件的 schema 版本。
///
/// 后续设置结构发生不兼容变化时，应通过版本迁移处理，
/// 而不是让新代码直接猜测旧数据结构。
pub const CURRENT_SETTINGS_SCHEMA_VERSION: u32 = 19;

pub const DEFAULT_SIDEBAR_WIDTH_PX: u16 = 280;

/// agent 界面语言偏好。
///
/// 注意这里描述的是“持久设置”，而不是当前 Agent
/// 在某一次对话中使用什么语言。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub enum InterfaceLanguage {
    /// 跟随操作系统语言。
    #[default]
    #[serde(rename = "system")]
    System,

    /// 强制使用英文界面。
    #[serde(rename = "en")]
    English,

    /// 强制使用简体中文界面。
    #[serde(rename = "zh-CN")]
    SimplifiedChinese,
}

/// 全局通用设置。
///
/// Stage 1.5A 只加入现在真正使用的字段。
/// Communication / Proactivity 等设置等对应功能实现时再扩充，
/// 避免提前制造没有业务行为的“假配置”。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GeneralSettings {
    pub interface_language: InterfaceLanguage,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelCapabilities {
    #[serde(default = "unknown_capability")]
    pub text: String,
    #[serde(default = "unknown_capability")]
    pub vision: String,
    #[serde(default = "unknown_capability")]
    pub vision_source: String,
    #[serde(default = "unknown_capability")]
    pub vision_status: String,
    #[serde(default)]
    pub vision_error_code: Option<String>,
    #[serde(default)]
    pub vision_verified_at: Option<String>,
    #[serde(default = "unknown_capability")]
    pub reasoning: String,
    #[serde(default = "unknown_capability")]
    pub tools: String,
    #[serde(default)]
    pub context_length: Option<u64>,
    #[serde(default)]
    pub max_output_tokens: Option<u64>,
    #[serde(default = "fallback_context_length_source")]
    pub context_length_source: String,
    #[serde(default)]
    pub input_price_per_million: Option<String>,
    #[serde(default)]
    pub output_price_per_million: Option<String>,
    #[serde(default)]
    pub routing_quality_score: Option<u8>,
    #[serde(default)]
    pub routing_priority: i8,
}

/// Capability state is intentionally independent from the model's general
/// admission status. A failed search route must not make text generation
/// unavailable for the same model.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelCapabilityState {
    pub capability_id: String,
    #[serde(default = "unknown_capability")]
    pub declared_status: String,
    #[serde(default = "unknown_capability")]
    pub verified_status: String,
    #[serde(default)]
    pub limits: BTreeMap<String, String>,
    #[serde(default)]
    pub last_verified_at: Option<String>,
    #[serde(default)]
    pub error_code: Option<String>,
}

fn unknown_capability() -> String {
    "unknown".to_string()
}

fn fallback_context_length_source() -> String {
    "fallback".to_string()
}

impl Default for ModelCapabilities {
    fn default() -> Self {
        Self {
            text: unknown_capability(),
            vision: unknown_capability(),
            vision_source: unknown_capability(),
            vision_status: unknown_capability(),
            vision_error_code: None,
            vision_verified_at: None,
            reasoning: unknown_capability(),
            tools: unknown_capability(),
            context_length: None,
            max_output_tokens: None,
            context_length_source: fallback_context_length_source(),
            input_price_per_million: None,
            output_price_per_million: None,
            routing_quality_score: None,
            routing_priority: 0,
        }
    }
}

pub(crate) fn apply_vision_declaration(
    preset_id: &str,
    model_id: &str,
    capabilities: &mut ModelCapabilities,
) {
    if capabilities.vision_source == "provider" && capabilities.vision == "yes" {
        capabilities.vision_source = "provider".to_string();
        capabilities.vision_status = "declared".to_string();
        return;
    }
    if capabilities.vision_source == "provider" && capabilities.vision == "no" {
        capabilities.vision_source = "provider".to_string();
        capabilities.vision_status = "unsupported".to_string();
        return;
    }
    if matches!(
        capabilities.vision_source.as_str(),
        "user_confirmed" | "runtime"
    ) {
        return;
    }
    // Fresh provider discovery predates the richer source field. A definite
    // catalog value is authoritative; persisted manifest values are recalculated.
    if capabilities.vision_source == "unknown" && capabilities.vision == "yes" {
        capabilities.vision_source = "provider".to_string();
        capabilities.vision_status = "declared".to_string();
        return;
    }
    if capabilities.vision_source == "unknown" && capabilities.vision == "no" {
        capabilities.vision_source = "provider".to_string();
        capabilities.vision_status = "unsupported".to_string();
        return;
    }
    let normalized = model_id.to_ascii_lowercase();
    let declared = match preset_id {
        "builtin_demo" => normalized == "demo",
        "openai" => ["gpt-4o", "gpt-4.1", "gpt-5", "o1", "o3", "o4"]
            .iter()
            .any(|prefix| normalized.starts_with(prefix)),
        "moonshot_cn" | "kimi_global" => {
            normalized.starts_with("kimi-k2.5") || normalized.contains("vision")
        }
        // DeepSeek model generations are not assumed to be visual merely from
        // a future-facing version number. Only an explicit vision model name or
        // provider catalog declaration is safe to pre-authorize.
        "deepseek" => normalized == "deepseek-flash" || normalized.contains("vision"),
        "ollama" => [
            "llava",
            "bakllava",
            "moondream",
            "minicpm-v",
            "qwen2.5vl",
            "qwen3-vl",
            "gemma3",
            "llama3.2-vision",
        ]
        .iter()
        .any(|marker| normalized.contains(marker)),
        _ => false,
    };
    if declared {
        capabilities.vision = "yes".to_string();
        capabilities.vision_source = "official_manifest".to_string();
        capabilities.vision_status = "declared".to_string();
    } else {
        capabilities.vision_source = "unknown".to_string();
        capabilities.vision_status = "unknown".to_string();
    }
    capabilities.vision_error_code = None;
    capabilities.vision_verified_at = None;
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProviderModelSettings {
    pub id: String,
    pub source: String,
    pub status: String,
    pub enabled: bool,
    /// Manual admission and automatic routing are separate authorities.
    /// A model may remain manually selectable without being callable in the
    /// background by the automatic planner.
    #[serde(default)]
    pub allow_automatic_routing: bool,
    #[serde(default = "unknown_tool_mode")]
    pub tool_mode: String,
    #[serde(default)]
    pub capabilities: ModelCapabilities,
    #[serde(default)]
    pub capability_states: Vec<ModelCapabilityState>,
    #[serde(default)]
    pub last_seen_at: Option<String>,
    #[serde(default = "not_applicable_native_search", skip_serializing)]
    pub native_web_search_status: String,
    #[serde(default, skip_serializing)]
    pub native_web_search_error_code: Option<String>,
    #[serde(default, skip_serializing)]
    pub native_web_search_verified_at: Option<String>,
    #[serde(default)]
    pub route_capabilities: Vec<ModelRouteCapabilityState>,
    #[serde(default)]
    pub tool_contracts: Vec<ModelToolContractState>,
}

fn unknown_tool_mode() -> String {
    "unknown".to_string()
}

fn not_applicable_native_search() -> String {
    "not_applicable".to_string()
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NativeSearchBinding {
    pub adapter_id: String,
    pub protocol: String,
    #[serde(default)]
    pub endpoint_base_url: Option<String>,
    pub source: String,
    pub adapter_version: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct InvocationRouteBinding {
    pub route_id: String,
    pub route_role: String,
    pub protocol: String,
    pub adapter_id: String,
    #[serde(default)]
    pub endpoint_base_url: Option<String>,
    pub source: String,
    pub manifest_version: String,
    pub config_fingerprint: String,
    #[serde(default)]
    pub capabilities: Vec<String>,
    #[serde(default = "unknown_source_provenance")]
    pub source_provenance: String,
    #[serde(default = "default_tool_transports")]
    pub tool_transports: Vec<String>,
    #[serde(default)]
    pub preferred_tool_transport: Option<String>,
}

fn default_tool_transports() -> Vec<String> {
    vec!["none".to_string()]
}

fn unknown_source_provenance() -> String {
    "unknown".to_string()
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelRouteCapabilityState {
    pub route_id: String,
    #[serde(default)]
    pub capability_id: String,
    #[serde(default)]
    pub capability_signature: Vec<String>,
    #[serde(rename = "support_status", alias = "status")]
    pub status: String,
    #[serde(default, rename = "unsupported_reason_code", alias = "error_code")]
    pub error_code: Option<String>,
    #[serde(default)]
    pub verified_at: Option<String>,
    pub route_config_fingerprint: String,
    #[serde(default = "unknown_source_provenance")]
    pub evidence_kind: String,
    #[serde(default = "ready_route_health")]
    pub health_status: String,
    #[serde(default)]
    pub last_error_code: Option<String>,
}

fn ready_route_health() -> String {
    "ready".to_string()
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelToolContractState {
    pub route_id: String,
    pub transport: String,
    pub contract_version: String,
    pub schema_hash: String,
    pub status: String,
    #[serde(default)]
    pub verified_tool_names: Vec<String>,
    #[serde(default)]
    pub verified_at: Option<String>,
    #[serde(default)]
    pub last_error_code: Option<String>,
}

impl Default for NativeSearchBinding {
    fn default() -> Self {
        Self {
            adapter_id: "none".to_string(),
            protocol: "none".to_string(),
            endpoint_base_url: None,
            source: "official_preset".to_string(),
            adapter_version: "1".to_string(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProviderProfileSettings {
    pub id: String,
    pub name: String,
    pub kind: String,
    #[serde(default = "custom_preset")]
    pub preset_id: String,
    #[serde(default, alias = "base_url")]
    pub entered_base_url: String,
    #[serde(default)]
    pub resolved_api_base_url: Option<String>,
    #[serde(default = "legacy_endpoint_mode")]
    pub endpoint_mode: String,
    pub enabled: bool,
    pub credential_ref: Option<String>,
    #[serde(default)]
    pub models: Vec<ProviderModelSettings>,
    #[serde(default)]
    pub last_discovered_at: Option<String>,
    #[serde(default = "default_primary_protocol", skip_serializing)]
    pub primary_protocol: String,
    #[serde(default, skip_serializing)]
    pub native_search: NativeSearchBinding,
    #[serde(default)]
    pub invocation_routes: Vec<InvocationRouteBinding>,
}

fn default_primary_protocol() -> String {
    "openai_chat".to_string()
}

/// Resolve native web-search support from a trusted preset or an exact official endpoint.
///
/// Older Settings files predate `native_web_adapter`. Keeping this mapping in one place lets
/// migration and provider updates repair those profiles without treating arbitrary compatible
/// gateways as if they exposed a vendor-specific search protocol.
pub(crate) fn native_search_binding_for_profile(
    preset_id: &str,
    base_url: &str,
) -> (String, NativeSearchBinding) {
    let preset = match preset_id {
        "builtin_demo" => Some(("memory", "none", "none", None)),
        "openai" => Some((
            "openai_responses",
            "openai_responses_web_search",
            "openai_responses",
            None,
        )),
        "moonshot_cn" | "kimi_global" => Some((
            "openai_chat",
            "kimi_builtin_web_search",
            "openai_chat",
            None,
        )),
        "deepseek" => Some((
            "openai_chat",
            "anthropic_messages_web_search",
            "anthropic_messages",
            Some("https://api.deepseek.com/anthropic"),
        )),
        "ollama" => Some(("ollama", "none", "none", None)),
        _ => match url::Url::parse(base_url)
            .ok()
            .and_then(|url| url.host_str().map(str::to_ascii_lowercase))
            .as_deref()
        {
            Some("api.openai.com") => Some((
                "openai_responses",
                "openai_responses_web_search",
                "openai_responses",
                None,
            )),
            Some("api.moonshot.cn" | "api.moonshot.ai") => Some((
                "openai_chat",
                "kimi_builtin_web_search",
                "openai_chat",
                None,
            )),
            Some("api.deepseek.com") => Some((
                "openai_chat",
                "anthropic_messages_web_search",
                "anthropic_messages",
                Some("https://api.deepseek.com/anthropic"),
            )),
            _ => None,
        },
    };
    let Some((primary, adapter, protocol, endpoint)) = preset else {
        return (
            "openai_chat".to_string(),
            NativeSearchBinding {
                source: "manual".to_string(),
                ..NativeSearchBinding::default()
            },
        );
    };
    (
        primary.to_string(),
        NativeSearchBinding {
            adapter_id: adapter.to_string(),
            protocol: protocol.to_string(),
            endpoint_base_url: endpoint.map(str::to_string),
            source: "official_preset".to_string(),
            adapter_version: "1".to_string(),
        },
    )
}

fn route_fingerprint(
    route_id: &str,
    protocol: &str,
    adapter_id: &str,
    endpoint_base_url: Option<&str>,
    manifest_version: &str,
    source_provenance: &str,
) -> String {
    let mut digest = Sha256::new();
    for value in [
        route_id,
        protocol,
        adapter_id,
        endpoint_base_url.unwrap_or(""),
        manifest_version,
        source_provenance,
    ] {
        digest.update(value.as_bytes());
        digest.update([0]);
    }
    format!("{:x}", digest.finalize())
}

fn route(
    route_id: &str,
    role: &str,
    protocol: &str,
    adapter_id: &str,
    endpoint: Option<&str>,
    source: &str,
    capabilities: &[&str],
) -> InvocationRouteBinding {
    let version = "unified-invocation-routes-v5";
    let source_provenance = match adapter_id {
        "openai.responses"
        | "deepseek.anthropic_web"
        | "anthropic.messages"
        | "tavily.search"
        | "searxng.search" => "structured",
        "kimi.openai_chat" | "kimi.web.builtin" | "kimi.formula_search" | "kimi.web.formula" => {
            "opaque"
        }
        _ if source == "official_preset" => "none",
        _ => "unknown",
    };
    InvocationRouteBinding {
        route_id: route_id.to_string(),
        route_role: role.to_string(),
        protocol: protocol.to_string(),
        adapter_id: adapter_id.to_string(),
        endpoint_base_url: endpoint.map(str::to_string),
        source: source.to_string(),
        manifest_version: version.to_string(),
        config_fingerprint: route_fingerprint(
            route_id,
            protocol,
            adapter_id,
            endpoint,
            version,
            source_provenance,
        ),
        capabilities: capabilities
            .iter()
            .map(|value| (*value).to_string())
            .collect(),
        source_provenance: source_provenance.to_string(),
        tool_transports: if capabilities.contains(&"function_tools") {
            vec![
                "native_function_call".to_string(),
                "structured_json".to_string(),
            ]
        } else {
            vec!["none".to_string()]
        },
        preferred_tool_transport: if capabilities.contains(&"function_tools") {
            Some("native_function_call".to_string())
        } else {
            None
        },
    }
}

pub(crate) fn invocation_routes_for_profile(
    preset_id: &str,
    base_url: &str,
    custom_protocol: Option<&str>,
) -> Vec<InvocationRouteBinding> {
    let official_preset = match preset_id {
        "builtin_demo" => Some("builtin_demo"),
        "openai" => Some("openai"),
        "moonshot_cn" | "kimi_global" => Some("kimi"),
        "deepseek" => Some("deepseek"),
        "ollama" => Some("ollama"),
        _ => url::Url::parse(base_url)
            .ok()
            .and_then(|url| url.host_str().map(str::to_ascii_lowercase))
            .and_then(|host| match host.as_str() {
                "api.openai.com" => Some("openai"),
                "api.moonshot.cn" | "api.moonshot.ai" => Some("kimi"),
                "api.deepseek.com" => Some("deepseek"),
                _ => None,
            }),
    };
    match official_preset {
        Some("builtin_demo") => vec![route(
            "builtin.memory",
            "primary",
            "memory",
            "fake.deterministic",
            None,
            "official_preset",
            &["text", "vision", "function_tools", "reasoning"],
        )],
        Some("openai") => vec![route(
            "openai.responses",
            "primary",
            "openai_responses",
            "openai.responses",
            None,
            "official_preset",
            &[
                "text",
                "vision",
                "function_tools",
                "native_web_search",
                "reasoning",
            ],
        )],
        Some("kimi") => vec![
            route(
                "kimi.openai_chat",
                "primary",
                "openai_chat",
                "kimi.chat.openai",
                None,
                "official_preset",
                &["text", "vision", "function_tools", "reasoning"],
            ),
            route(
                "kimi.web.builtin",
                "native_search",
                "openai_chat",
                "kimi.web.builtin",
                None,
                "official_preset",
                &["text", "native_web_search"],
            ),
            route(
                "kimi.web.formula",
                "native_search",
                "openai_chat",
                "kimi.web.formula",
                None,
                "official_preset",
                &["text", "native_web_search"],
            ),
        ],
        Some("deepseek") => vec![
            route(
                "deepseek.openai_chat",
                "primary",
                "openai_chat",
                "deepseek.openai_chat",
                None,
                "official_preset",
                &["text", "function_tools", "reasoning"],
            ),
            route(
                "deepseek.responses",
                "vision",
                "openai_responses",
                "deepseek.responses",
                None,
                "official_preset",
                &["text", "vision", "reasoning"],
            ),
            route(
                "deepseek.anthropic_web",
                "native_search",
                "anthropic_messages",
                "deepseek.anthropic_web",
                Some("https://api.deepseek.com/anthropic"),
                "official_preset",
                &["text", "native_web_search"],
            ),
        ],
        Some("ollama") => vec![route(
            "ollama.generate",
            "primary",
            "ollama",
            "ollama.chat",
            None,
            "official_preset",
            &["text", "vision", "function_tools", "reasoning"],
        )],
        _ => {
            let protocol = custom_protocol.unwrap_or("openai_chat");
            vec![route(
                "custom.primary",
                "primary",
                protocol,
                protocol,
                None,
                "manual",
                &["text", "vision", "function_tools", "reasoning"],
            )]
        }
    }
}

pub(crate) fn route_capabilities_for_model(
    routes: &[InvocationRouteBinding],
    legacy_native_status: &str,
) -> Vec<ModelRouteCapabilityState> {
    routes
        .iter()
        .flat_map(|route| {
            let mut signatures = vec![vec!["text".to_string()]];
            for capability in &route.capabilities {
                if capability != "text" {
                    signatures.push(vec!["text".to_string(), capability.clone()]);
                }
            }
            let supports = |value: &str| route.capabilities.iter().any(|item| item == value);
            if supports("vision")
                && supports("function_tools")
                && matches!(
                    route.protocol.as_str(),
                    "memory" | "ollama" | "openai_chat" | "openai_responses"
                )
            {
                signatures.push(vec![
                    "text".to_string(),
                    "vision".to_string(),
                    "function_tools".to_string(),
                ]);
            }
            if supports("function_tools") && supports("reasoning") {
                signatures.push(vec![
                    "text".to_string(),
                    "function_tools".to_string(),
                    "reasoning".to_string(),
                ]);
            }
            if supports("vision") && supports("reasoning") {
                signatures.push(vec![
                    "text".to_string(),
                    "vision".to_string(),
                    "reasoning".to_string(),
                ]);
            }
            signatures.into_iter().map(|mut signature| {
                signature.sort();
                let is_native = signature
                    .iter()
                    .any(|value| value == "native_web_search" || value == "web_search");
                let capability_id = if signature
                    .iter()
                    .any(|value| value == "structured_web_evidence")
                {
                    "structured_web_evidence"
                } else if is_native {
                    "web_search"
                } else if signature
                    .iter()
                    .any(|value| value == "vision" || value == "vision_input")
                {
                    "vision_input"
                } else if signature
                    .iter()
                    .any(|value| value == "function_tools" || value == "function_calling")
                {
                    "function_calling"
                } else if signature.iter().any(|value| value == "reasoning") {
                    "reasoning"
                } else {
                    "text_generation"
                };
                ModelRouteCapabilityState {
                    route_id: route.route_id.clone(),
                    capability_id: capability_id.to_string(),
                    capability_signature: signature,
                    status: if is_native {
                        match legacy_native_status {
                            "verified" => "verified",
                            "unsupported" => "unsupported",
                            _ => "declared",
                        }
                    } else {
                        "declared"
                    }
                    .to_string(),
                    error_code: None,
                    verified_at: None,
                    route_config_fingerprint: route.config_fingerprint.clone(),
                    evidence_kind: if capability_id == "structured_web_evidence" {
                        "structured".to_string()
                    } else if is_native {
                        if route.source_provenance == "structured" {
                            "structured".to_string()
                        } else {
                            "opaque".to_string()
                        }
                    } else {
                        "none".to_string()
                    },
                    health_status: "ready".to_string(),
                    last_error_code: None,
                }
            })
        })
        .collect()
}

impl ProviderProfileSettings {
    pub fn effective_base_url(&self) -> &str {
        self.resolved_api_base_url
            .as_deref()
            .unwrap_or(&self.entered_base_url)
    }

    pub fn allowed_model_ids(&self) -> Vec<String> {
        self.models
            .iter()
            .filter(|model| model.enabled && model.status != "unavailable")
            .map(|model| model.id.clone())
            .collect()
    }
}

fn custom_preset() -> String {
    "custom".to_string()
}
fn legacy_endpoint_mode() -> String {
    "legacy_auto_v1".to_string()
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelSettings {
    #[serde(default = "default_provider_profiles")]
    pub providers: Vec<ProviderProfileSettings>,
    #[serde(default = "default_provider_id")]
    pub default_provider_profile_id: String,
    #[serde(default = "default_model_id")]
    pub default_model_id: String,
    #[serde(default = "default_connect_timeout")]
    pub connect_timeout_secs: u16,
    #[serde(default = "default_first_output_timeout")]
    pub first_output_timeout_secs: u16,
    #[serde(default = "default_chunk_timeout")]
    pub chunk_timeout_secs: u16,
    #[serde(default = "default_total_timeout")]
    pub total_timeout_secs: u16,
}

fn default_provider_profiles() -> Vec<ProviderProfileSettings> {
    let invocation_routes = invocation_routes_for_profile("builtin_demo", "memory://fake", None);
    vec![ProviderProfileSettings {
        id: "builtin-demo".to_string(),
        name: "Built-in Demo".to_string(),
        kind: "fake".to_string(),
        preset_id: "builtin_demo".to_string(),
        entered_base_url: "memory://fake".to_string(),
        resolved_api_base_url: Some("memory://fake".to_string()),
        endpoint_mode: "preset".to_string(),
        enabled: true,
        credential_ref: None,
        models: vec![ProviderModelSettings {
            id: "demo".to_string(),
            source: "discovered".to_string(),
            status: "available".to_string(),
            enabled: true,
            allow_automatic_routing: false,
            tool_mode: "native".to_string(),
            capabilities: ModelCapabilities {
                text: "yes".to_string(),
                ..Default::default()
            },
            capability_states: Vec::new(),
            last_seen_at: None,
            native_web_search_status: "not_applicable".to_string(),
            native_web_search_error_code: None,
            native_web_search_verified_at: None,
            route_capabilities: route_capabilities_for_model(&invocation_routes, "not_applicable"),
            tool_contracts: Vec::new(),
        }],
        last_discovered_at: None,
        primary_protocol: "memory".to_string(),
        native_search: NativeSearchBinding::default(),
        invocation_routes,
    }]
}
fn default_provider_id() -> String {
    "builtin-demo".to_string()
}
fn default_model_id() -> String {
    "demo".to_string()
}
const fn default_connect_timeout() -> u16 {
    15
}
const fn default_first_output_timeout() -> u16 {
    60
}
const fn default_chunk_timeout() -> u16 {
    30
}
const fn default_total_timeout() -> u16 {
    600
}

impl Default for ModelSettings {
    fn default() -> Self {
        Self {
            providers: default_provider_profiles(),
            default_provider_profile_id: default_provider_id(),
            default_model_id: default_model_id(),
            connect_timeout_secs: default_connect_timeout(),
            first_output_timeout_secs: default_first_output_timeout(),
            chunk_timeout_secs: default_chunk_timeout(),
            total_timeout_secs: default_total_timeout(),
        }
    }
}

impl ModelSettings {
    pub(crate) fn remove_demo_provider(&mut self) {
        let demo_default = self.default_provider_profile_id == "builtin-demo"
            || self.providers.iter().any(|profile| {
                profile.kind == "fake" && profile.id == self.default_provider_profile_id
            });
        self.providers
            .retain(|profile| profile.id != "builtin-demo" && profile.kind != "fake");
        if demo_default {
            self.default_provider_profile_id.clear();
            self.default_model_id.clear();
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SidebarPreferences {
    #[serde(default)]
    pub standard_chats_collapsed: bool,
    #[serde(default)]
    pub projects_collapsed: bool,
    #[serde(default)]
    pub collapsed_chat_group_ids: Vec<String>,
    #[serde(default)]
    pub collapsed_project_ids: Vec<String>,
    #[serde(default = "default_sidebar_width_px")]
    pub width_px: u16,
}

const fn default_sidebar_width_px() -> u16 {
    DEFAULT_SIDEBAR_WIDTH_PX
}

impl Default for SidebarPreferences {
    fn default() -> Self {
        Self {
            standard_chats_collapsed: false,
            projects_collapsed: false,
            collapsed_chat_group_ids: Vec::new(),
            collapsed_project_ids: Vec::new(),
            width_px: DEFAULT_SIDEBAR_WIDTH_PX,
        }
    }
}

impl Default for GeneralSettings {
    fn default() -> Self {
        Self {
            interface_language: InterfaceLanguage::System,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum RemoteImagePolicy {
    #[default]
    Ask,
    Allow,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct ContentPreferences {
    #[serde(default)]
    pub remote_image_policy: RemoteImagePolicy,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SearchBackendSettings {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub base_url: String,
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub credential_ref: Option<String>,
    #[serde(default)]
    pub last_status: Option<String>,
    #[serde(default)]
    pub last_tested_at: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WebSearchSettings {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub privacy_acknowledged: bool,
    #[serde(default)]
    pub default_backend_id: Option<String>,
    #[serde(default = "default_safe_search")]
    pub safe_search: bool,
    #[serde(default = "default_search_language")]
    pub default_language: String,
    #[serde(default = "default_search_region")]
    pub default_region: String,
    #[serde(default)]
    pub backends: Vec<SearchBackendSettings>,
}

const fn default_safe_search() -> bool {
    true
}
fn default_search_language() -> String {
    "auto".to_string()
}
fn default_search_region() -> String {
    "auto".to_string()
}

impl Default for WebSearchSettings {
    fn default() -> Self {
        Self {
            enabled: false,
            privacy_acknowledged: false,
            default_backend_id: None,
            safe_search: true,
            default_language: default_search_language(),
            default_region: default_search_region(),
            backends: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RoutingPreferences {
    #[serde(default = "automatic_conversation_mode")]
    pub new_conversation_mode: String,
    #[serde(default = "balanced_routing_preset")]
    pub default_preset: String,
    #[serde(default = "default_model_call_budget")]
    pub max_model_calls_per_message: u8,
    #[serde(default = "default_specialist_budget")]
    pub max_specialist_steps: u8,
    #[serde(default = "default_extra_online_cost")]
    pub max_extra_online_cost_usd: String,
    #[serde(default)]
    pub allow_unknown_price_automatic: bool,
    #[serde(default = "default_intent_timeout")]
    pub intent_timeout_seconds: u32,
    #[serde(default = "default_model_idle")]
    pub main_model_idle_seconds: u32,
    #[serde(default = "default_model_idle")]
    pub auxiliary_model_idle_seconds: u32,
}

const fn default_intent_timeout() -> u32 {
    90
}
const fn default_model_idle() -> u32 {
    1800
}

fn automatic_conversation_mode() -> String {
    "automatic".to_string()
}
fn balanced_routing_preset() -> String {
    "balanced".to_string()
}
const fn default_model_call_budget() -> u8 {
    3
}
const fn default_specialist_budget() -> u8 {
    2
}
fn default_extra_online_cost() -> String {
    "0.20".to_string()
}

impl Default for RoutingPreferences {
    fn default() -> Self {
        Self {
            new_conversation_mode: automatic_conversation_mode(),
            default_preset: balanced_routing_preset(),
            max_model_calls_per_message: default_model_call_budget(),
            max_specialist_steps: default_specialist_budget(),
            max_extra_online_cost_usd: default_extra_online_cost(),
            allow_unknown_price_automatic: false,
            intent_timeout_seconds: default_intent_timeout(),
            main_model_idle_seconds: default_model_idle(),
            auxiliary_model_idle_seconds: default_model_idle(),
        }
    }
}

/// agent 全局应用设置的根对象。
///
/// 前端以后读取的正式数据结构就是这个类型。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AppSettings {
    pub schema_version: u32,
    pub general: GeneralSettings,
    #[serde(default)]
    pub sidebar: SidebarPreferences,
    #[serde(default)]
    pub models: ModelSettings,
    #[serde(default)]
    pub content: ContentPreferences,
    #[serde(default)]
    pub web_search: WebSearchSettings,
    #[serde(default)]
    pub routing: RoutingPreferences,
}

impl Default for AppSettings {
    fn default() -> Self {
        Self {
            schema_version: CURRENT_SETTINGS_SCHEMA_VERSION,
            general: GeneralSettings::default(),
            sidebar: SidebarPreferences::default(),
            models: ModelSettings::default(),
            content: ContentPreferences::default(),
            web_search: WebSearchSettings::default(),
            routing: RoutingPreferences::default(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_settings_follow_system_language() {
        let settings = AppSettings::default();

        assert_eq!(settings.schema_version, CURRENT_SETTINGS_SCHEMA_VERSION);

        assert_eq!(
            settings.general.interface_language,
            InterfaceLanguage::System
        );
        assert_eq!(settings.content.remote_image_policy, RemoteImagePolicy::Ask);
    }

    #[test]
    fn interface_language_uses_stable_external_values() {
        assert_eq!(
            serde_json::to_string(&InterfaceLanguage::System).unwrap(),
            "\"system\""
        );

        assert_eq!(
            serde_json::to_string(&InterfaceLanguage::English).unwrap(),
            "\"en\""
        );

        assert_eq!(
            serde_json::to_string(&InterfaceLanguage::SimplifiedChinese).unwrap(),
            "\"zh-CN\""
        );
    }

    #[test]
    fn interface_language_deserializes_from_frontend_values() {
        assert_eq!(
            serde_json::from_str::<InterfaceLanguage>("\"system\"").unwrap(),
            InterfaceLanguage::System
        );

        assert_eq!(
            serde_json::from_str::<InterfaceLanguage>("\"en\"").unwrap(),
            InterfaceLanguage::English
        );

        assert_eq!(
            serde_json::from_str::<InterfaceLanguage>("\"zh-CN\"").unwrap(),
            InterfaceLanguage::SimplifiedChinese
        );
    }

    #[test]
    fn official_presets_bind_native_search_without_a_probe() {
        let (protocol, deepseek) =
            native_search_binding_for_profile("deepseek", "https://api.deepseek.com");
        assert_eq!(protocol, "openai_chat");
        assert_eq!(deepseek.adapter_id, "anthropic_messages_web_search");
        assert_eq!(deepseek.protocol, "anthropic_messages");
        assert_eq!(
            deepseek.endpoint_base_url.as_deref(),
            Some("https://api.deepseek.com/anthropic")
        );

        let (_, custom) = native_search_binding_for_profile("custom", "https://gateway.example/v1");
        assert_eq!(custom.adapter_id, "none");
        assert_eq!(custom.source, "manual");
    }

    #[test]
    fn deepseek_version_names_do_not_imply_image_support() {
        let mut capabilities = ModelCapabilities::default();

        apply_vision_declaration("deepseek", "deepseek-v4-pro", &mut capabilities);

        assert_eq!(capabilities.vision, "unknown");
        assert_eq!(capabilities.vision_source, "unknown");
        assert_eq!(capabilities.vision_status, "unknown");

        apply_vision_declaration("deepseek", "deepseek-vision", &mut capabilities);

        assert_eq!(capabilities.vision, "yes");
        assert_eq!(capabilities.vision_source, "official_manifest");
        assert_eq!(capabilities.vision_status, "declared");
    }

    #[test]
    fn route_states_declare_precise_capability_signatures() {
        let routes = invocation_routes_for_profile("openai", "https://api.openai.com", None);
        let states = route_capabilities_for_model(&routes, "declared");
        assert!(states
            .iter()
            .any(|state| { state.capability_signature == ["native_web_search", "text"] }));
        assert!(states
            .iter()
            .any(|state| { state.capability_signature == ["text", "vision"] }));
        assert!(!states.iter().any(|state| {
            state
                .capability_signature
                .iter()
                .any(|value| value == "vision")
                && state
                    .capability_signature
                    .iter()
                    .any(|value| value == "native_web_search")
        }));
    }

    #[test]
    fn official_routes_declare_source_provenance_without_guessing_custom_gateways() {
        let deepseek = invocation_routes_for_profile("deepseek", "https://api.deepseek.com", None);
        let kimi = invocation_routes_for_profile("kimi", "https://api.moonshot.cn/v1", None);
        let custom = invocation_routes_for_profile("custom", "https://gateway.example/v1", None);
        assert_eq!(
            deepseek
                .iter()
                .find(|route| route.adapter_id == "deepseek.anthropic_web")
                .unwrap()
                .source_provenance,
            "structured",
        );
        assert_eq!(
            kimi.iter()
                .find(|route| route.adapter_id == "kimi.web.builtin")
                .unwrap()
                .source_provenance,
            "opaque",
        );
        assert!(custom
            .iter()
            .all(|route| route.source_provenance == "unknown"));
    }
}
