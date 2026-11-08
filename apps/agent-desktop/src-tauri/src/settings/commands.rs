use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use serde_json::json;
use tauri::{AppHandle, Manager, State};

use crate::{
    core_bridge::{request_core, request_core_with_timeout},
    core_process_manager::CoreProcessManager,
    credentials::CredentialStore,
    route_health::RouteHealthRegistry,
};

use super::{
    manager::SettingsManager,
    model::{
        apply_vision_declaration, invocation_routes_for_profile, native_search_binding_for_profile,
        route_capabilities_for_model, AppSettings, InterfaceLanguage, ModelCapabilities,
        ProviderModelSettings, ProviderProfileSettings, RemoteImagePolicy, RoutingPreferences,
        SidebarPreferences,
    },
};

/// 返回 agent 当前完整全局设置。
///
/// React 只通过这个 command 获取配置，
/// 不应该直接读取 settings.json。
#[tauri::command]
pub async fn get_app_settings(
    app: AppHandle,
    manager: State<'_, SettingsManager>,
    core: State<'_, Mutex<CoreProcessManager>>,
) -> Result<AppSettings, String> {
    catalog_settings(&app, &core, &manager).await
}

pub(crate) fn apply_catalog(
    mut settings: AppSettings,
    catalog: &serde_json::Value,
) -> Result<AppSettings, String> {
    settings.web_search.enabled = false;
    settings.content.remote_image_policy = RemoteImagePolicy::Ask;
    settings
        .models
        .providers
        .retain(|profile| !profile.id.starts_with("managed:"));
    for profile in &mut settings.models.providers {
        if crate::offline::validate_profile(profile).is_err() {
            profile.enabled = false;
        }
    }
    for model in catalog["models"]
        .as_array()
        .ok_or("Invalid local model catalog")?
    {
        let id = model["id"].as_str().ok_or("Invalid managed model ID")?;
        if id.len() != 64
            || !id
                .bytes()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        {
            return Err("Invalid managed model ID".into());
        }
        let provider_id = format!("managed:{id}");
        if model["provider_profile_id"].as_str() != Some(&provider_id) {
            return Err("Invalid managed provider ID".into());
        }
        let loaded = model["status"] == "loaded";
        let capabilities = ModelCapabilities {
            text: "yes".into(),
            context_length: model["context_length"].as_u64(),
            context_length_source: "discovered".into(),
            input_price_per_million: Some("0".into()),
            output_price_per_million: Some("0".into()),
            ..Default::default()
        };
        let profile: ProviderProfileSettings = serde_json::from_value(json!({
            "id": provider_id, "name": model["name"].as_str().unwrap_or(id),
            "kind": "openai_compatible", "preset_id": "custom", "entered_base_url": "",
            "resolved_api_base_url": null, "endpoint_mode": "exact", "enabled": true,
            "credential_ref": null, "invocation_routes": [],
            "models": [{ "id": id, "source": "discovered", "status": if loaded { "available" } else { "unavailable" },
                "enabled": true, "allow_automatic_routing": true,
                "tool_mode": model["tool_mode"].as_str().unwrap_or("unknown"), "capabilities": capabilities,
                "last_seen_at": null }]
        })).map_err(|_| "Invalid managed model metadata")?;
        if model["is_default"] == true {
            settings.models.default_provider_profile_id = provider_id;
            settings.models.default_model_id = id.to_string();
        }
        settings.models.providers.push(profile);
    }
    Ok(settings)
}

pub(crate) async fn catalog_settings(
    app: &AppHandle,
    core: &State<'_, Mutex<CoreProcessManager>>,
    manager: &SettingsManager,
) -> Result<AppSettings, String> {
    let settings = manager.get_settings().map_err(|error| error.to_string())?;
    let mut catalog = request_core(
        app,
        core,
        "local_model.request",
        json!({"action": "model.list", "params": {}}),
    )
    .await?;
    if catalog["models"]
        .as_array()
        .is_some_and(|models| models.iter().any(|model| model["status"] == "loaded"))
    {
        let runtime = request_core(
            app,
            core,
            "local_model.request",
            json!({"action": "runtime.status", "params": {}}),
        )
        .await?;
        if let Some(models) = catalog["models"].as_array_mut() {
            for model in models
                .iter_mut()
                .filter(|model| model["status"] == "loaded")
            {
                if runtime["state"] != "loaded" || runtime["model_id"] != model["id"] {
                    model["status"] = json!("imported");
                }
            }
        }
    }
    apply_catalog(settings, &catalog)
}

/// 修改全局界面语言。
///
/// 返回修改后的完整设置对象，
/// 避免前端自行猜测最终持久化状态。
#[tauri::command]
pub fn update_interface_language(
    language: InterfaceLanguage,

    manager: State<'_, SettingsManager>,
) -> Result<AppSettings, String> {
    manager
        .update_interface_language(language)
        .map_err(|error| error.to_string())
}

#[tauri::command]
pub fn update_sidebar_preferences(
    preferences: SidebarPreferences,
    manager: State<'_, SettingsManager>,
) -> Result<AppSettings, String> {
    manager
        .update_sidebar_preferences(preferences)
        .map_err(|error| error.to_string())
}

#[tauri::command]
pub fn update_sidebar_width(
    width_px: u16,
    manager: State<'_, SettingsManager>,
) -> Result<AppSettings, String> {
    manager
        .update_sidebar_width(width_px)
        .map_err(|error| error.to_string())
}

#[tauri::command]
pub fn update_remote_image_policy(
    policy: RemoteImagePolicy,
    manager: State<'_, SettingsManager>,
) -> Result<AppSettings, String> {
    if policy == RemoteImagePolicy::Allow {
        crate::offline::deny_network()?;
    }
    manager
        .update_remote_image_policy(policy)
        .map_err(|error| error.to_string())
}

#[tauri::command]
pub async fn update_routing_preferences(
    app: AppHandle,
    routing: RoutingPreferences,
    manager: State<'_, SettingsManager>,
    core: State<'_, Mutex<CoreProcessManager>>,
) -> Result<AppSettings, String> {
    let settings = manager
        .update_routing_preferences(routing)
        .map_err(|error| error.to_string())?;
    request_core(
        &app,
        &core,
        "routing.configure",
        routing_configuration(&settings),
    )
    .await?;
    Ok(settings)
}

#[derive(Deserialize)]
pub struct ProviderProfileInput {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub preset_id: String,
    pub entered_base_url: String,
    pub enabled: bool,
    pub api_key: Option<String>,
    #[serde(default)]
    pub primary_protocol: Option<String>,
}

fn preset_address(preset_id: &str, entered: &str) -> Result<(String, String), String> {
    let value = match preset_id {
        "ollama" => ("http://127.0.0.1:11434".to_string(), "preset".to_string()),
        "deepseek" => ("https://api.deepseek.com".to_string(), "preset".to_string()),
        "openai" => (
            "https://api.openai.com/v1".to_string(),
            "preset".to_string(),
        ),
        "moonshot_cn" => (
            "https://api.moonshot.cn/v1".to_string(),
            "preset".to_string(),
        ),
        "kimi_global" => (
            "https://api.moonshot.ai/v1".to_string(),
            "preset".to_string(),
        ),
        "custom" => (
            entered.trim_end_matches('/').to_string(),
            "exact".to_string(),
        ),
        _ => return Err("The provider preset is not supported".to_string()),
    };
    Ok(value)
}

fn validate_provider_url(kind: &str, value: &str) -> Result<(), String> {
    if kind == "fake" && value == "memory://fake" {
        return Ok(());
    }
    crate::offline::local_endpoint(value).map(|_| ())
}

async fn inject_provider(
    app: &AppHandle,
    core: &State<'_, Mutex<CoreProcessManager>>,
    credentials: &State<'_, CredentialStore>,
    profile: &ProviderProfileSettings,
) -> Result<(), String> {
    if profile.id.starts_with("managed:") {
        return Ok(());
    }
    crate::offline::validate_profile(profile)?;
    let api_key = if profile.credential_ref.is_some() {
        credentials.get(&profile.id)?
    } else {
        None
    };
    let route_health = app
        .state::<Mutex<RouteHealthRegistry>>()
        .lock()
        .map_err(|_| "route health registry lock failed".to_string())?
        .snapshot()
        .into_iter()
        .filter(|record| {
            record.key.target_kind == "model" && record.key.provider_or_backend_id == profile.id
        })
        .fold(
            std::collections::HashMap::<String, Vec<_>>::new(),
            |mut grouped, record| {
                if let Some(model_id) = record.key.model_id.clone() {
                    grouped.entry(model_id).or_default().push(record);
                }
                grouped
            },
        );
    request_core(
        app,
        core,
        "provider.configure",
        json!({
            "id": profile.id, "name": profile.name, "kind": profile.kind,
            "base_url": profile.effective_base_url(), "enabled": profile.enabled, "api_key": api_key,
            "endpoint_mode": profile.endpoint_mode,
            "allowed_model_ids": profile.allowed_model_ids(),
            "automatic_model_ids": profile.models.iter()
                .filter(|model| profile.kind != "fake" && model.enabled
                    && model.allow_automatic_routing && model.status != "unavailable")
                .map(|model| model.id.clone())
                .collect::<Vec<_>>(),
            "model_capabilities": profile.models.iter()
                .filter(|model| model.enabled && model.status != "unavailable")
                .map(|model| (model.id.clone(), model.capabilities.clone()))
                .collect::<std::collections::HashMap<_, _>>(),
            "model_capability_states": profile.models.iter()
                .filter(|model| model.enabled && model.status != "unavailable")
                .map(|model| (model.id.clone(), model.capability_states.clone()))
                .collect::<std::collections::HashMap<_, _>>(),
            "model_tool_modes": profile.models.iter()
                .filter(|model| model.enabled && model.status != "unavailable")
                .map(|model| (model.id.clone(), model.tool_mode.clone()))
                .collect::<std::collections::HashMap<_, _>>(),
            "primary_protocol": profile.primary_protocol,
            "native_search": profile.native_search,
            "model_native_web_search": profile.models.iter()
                .filter(|model| model.enabled && model.status != "unavailable")
                .map(|model| (model.id.clone(), model.native_web_search_status.clone()))
                .collect::<std::collections::HashMap<_, _>>(),
            "invocation_routes": profile.invocation_routes,
            "model_route_capabilities": profile.models.iter()
                .filter(|model| model.enabled && model.status != "unavailable")
                .map(|model| (model.id.clone(), model.route_capabilities.clone()))
                .collect::<std::collections::HashMap<_, _>>(),
            "model_route_health": route_health,
        }),
    )
    .await?;
    Ok(())
}

pub(crate) fn routing_configuration(settings: &AppSettings) -> serde_json::Value {
    let mut value = serde_json::to_value(&settings.routing).unwrap_or_else(|_| json!({}));
    if let Some(object) = value.as_object_mut() {
        object.insert(
            "global_default_provider_profile_id".to_string(),
            json!(settings.models.default_provider_profile_id),
        );
        object.insert(
            "global_default_model_id".to_string(),
            json!(settings.models.default_model_id),
        );
    }
    value
}

pub async fn reinject_providers(app: &AppHandle) -> Result<(), String> {
    let manager = app.state::<SettingsManager>();
    let core = app.state::<Mutex<CoreProcessManager>>();
    let credentials = app.state::<CredentialStore>();
    let settings = manager.get_settings().map_err(|error| error.to_string())?;
    let profiles = &settings.models.providers;
    for profile in profiles.iter().filter(|profile| {
        profile.enabled
            && !profile.id.starts_with("managed:")
            && crate::offline::validate_profile(profile).is_ok()
    }) {
        inject_provider(app, &core, &credentials, profile).await?;
    }
    request_core(
        app,
        &core,
        "routing.configure",
        routing_configuration(&settings),
    )
    .await?;
    Ok(())
}

#[tauri::command]
pub async fn save_model_provider(
    app: AppHandle,
    manager: State<'_, SettingsManager>,
    core: State<'_, Mutex<CoreProcessManager>>,
    credentials: State<'_, CredentialStore>,
    provider: ProviderProfileInput,
) -> Result<AppSettings, String> {
    if provider.id.starts_with("managed:") {
        return Err("Managed model IDs are reserved.".into());
    }
    let (entered_base_url, endpoint_mode) =
        preset_address(&provider.preset_id, &provider.entered_base_url)?;
    validate_provider_url(&provider.kind, &entered_base_url)?;
    let previous_secret = credentials.get(&provider.id)?;
    if let Some(secret) = provider.api_key.as_deref() {
        if secret.is_empty() {
            credentials.delete(&provider.id)?;
        } else {
            credentials.set(&provider.id, secret)?;
        }
    }
    let existing = manager
        .get_settings()
        .map_err(|error| error.to_string())?
        .models
        .providers
        .into_iter()
        .find(|item| item.id == provider.id);
    let existing_ref = existing
        .as_ref()
        .and_then(|item| item.credential_ref.clone());
    let mut existing_models = existing
        .as_ref()
        .map(|item| item.models.clone())
        .unwrap_or_default();
    let last_discovered_at = existing
        .as_ref()
        .and_then(|item| item.last_discovered_at.clone());
    let (mut primary_protocol, native_search) =
        native_search_binding_for_profile(&provider.preset_id, &entered_base_url);
    if provider.preset_id == "custom" {
        primary_protocol = provider
            .primary_protocol
            .clone()
            .unwrap_or_else(|| "openai_chat".to_string());
        if !matches!(
            primary_protocol.as_str(),
            "openai_chat" | "openai_responses" | "anthropic_messages"
        ) {
            return Err("The custom provider protocol is not supported".to_string());
        }
    }
    let binding_changed = existing.as_ref().is_none_or(|item| {
        item.primary_protocol != primary_protocol || item.native_search != native_search
    });
    let invocation_routes = invocation_routes_for_profile(
        &provider.preset_id,
        &entered_base_url,
        if provider.preset_id == "custom" {
            Some(primary_protocol.as_str())
        } else {
            None
        },
    );
    if binding_changed {
        let status = if native_search.adapter_id == "none" {
            "not_applicable"
        } else {
            "declared"
        };
        for model in &mut existing_models {
            model.native_web_search_status = status.to_string();
            model.native_web_search_error_code = None;
            model.native_web_search_verified_at = None;
            model.capabilities.vision = "unknown".to_string();
            model.capabilities.vision_source = "unknown".to_string();
            model.capabilities.vision_status = "unknown".to_string();
            model.capabilities.vision_error_code = None;
            model.capabilities.vision_verified_at = None;
            apply_vision_declaration(&provider.preset_id, &model.id, &mut model.capabilities);
            model.route_capabilities = route_capabilities_for_model(&invocation_routes, status);
        }
    }
    let profile = ProviderProfileSettings {
        id: provider.id,
        name: provider.name,
        kind: provider.kind,
        preset_id: provider.preset_id,
        entered_base_url: entered_base_url.clone(),
        resolved_api_base_url: Some(entered_base_url),
        endpoint_mode,
        enabled: provider.enabled,
        credential_ref: match provider.api_key {
            Some(secret) if secret.is_empty() => None,
            Some(_) => Some("windows-credential-manager".to_string()),
            None => existing_ref,
        },
        models: existing_models,
        last_discovered_at,
        primary_protocol,
        native_search,
        invocation_routes,
    };
    let settings = match manager.upsert_provider(profile.clone()) {
        Ok(settings) => settings,
        Err(error) => {
            match previous_secret {
                Some(secret) => {
                    let _ = credentials.set(&profile.id, &secret);
                }
                None => {
                    let _ = credentials.delete(&profile.id);
                }
            }
            return Err(error.to_string());
        }
    };
    if let Ok(mut health) = app.state::<Mutex<RouteHealthRegistry>>().lock() {
        let _ = health.clear_configuration_blocks(&profile.id);
    }
    inject_provider(&app, &core, &credentials, &profile).await?;
    Ok(settings)
}

#[tauri::command]
pub async fn connect_model_provider(
    app: AppHandle,
    manager: State<'_, SettingsManager>,
    core: State<'_, Mutex<CoreProcessManager>>,
    credentials: State<'_, CredentialStore>,
    provider_id: String,
) -> Result<(), String> {
    let settings = catalog_settings(&app, &core, &manager).await?;
    let profile = settings
        .models
        .providers
        .iter()
        .find(|profile| profile.id == provider_id && profile.enabled)
        .ok_or("The provider profile is unavailable offline")?;
    if profile.id.starts_with("managed:")
        && !profile
            .models
            .iter()
            .any(|model| model.status == "available")
    {
        return Err("model_unavailable: Load this local model before chatting.".into());
    }
    inject_provider(&app, &core, &credentials, profile).await
}

#[tauri::command]
pub async fn refresh_provider_models(
    app: AppHandle,
    manager: State<'_, SettingsManager>,
    core: State<'_, Mutex<CoreProcessManager>>,
    credentials: State<'_, CredentialStore>,
    provider_id: String,
) -> Result<AppSettings, String> {
    let profile = manager
        .get_settings()
        .map_err(|error| error.to_string())?
        .models
        .providers
        .into_iter()
        .find(|item| item.id == provider_id)
        .ok_or_else(|| "The provider profile does not exist".to_string())?;
    inject_provider(&app, &core, &credentials, &profile).await?;
    let result = request_core(
        &app,
        &core,
        "provider.list_models",
        json!({"provider_profile_id": provider_id}),
    )
    .await?;
    let models = result
        .get("models")
        .and_then(|value| value.as_array())
        .ok_or_else(|| "The provider returned an invalid model catalog".to_string())?;
    let now = chrono::Utc::now().to_rfc3339();
    let discovered = models
        .iter()
        .filter_map(|value| {
            let id = value.get("id")?.as_str()?;
            let source = value.get("capabilities");
            let capability = |name: &str| {
                source
                    .and_then(|item| item.get(name))
                    .and_then(|item| item.as_str())
                    .unwrap_or("unknown")
                    .to_string()
            };
            let mut capabilities = ModelCapabilities {
                text: capability("text"),
                vision: capability("vision"),
                reasoning: capability("reasoning"),
                tools: capability("tools"),
                context_length: source
                    .and_then(|item| item.get("context_length"))
                    .and_then(|item| item.as_u64()),
                max_output_tokens: source
                    .and_then(|item| item.get("max_output_tokens"))
                    .and_then(|item| item.as_u64()),
                context_length_source: if source
                    .and_then(|item| item.get("context_length"))
                    .and_then(|item| item.as_u64())
                    .is_some()
                {
                    "discovered".to_string()
                } else {
                    "fallback".to_string()
                },
                ..ModelCapabilities::default()
            };
            apply_vision_declaration(&profile.preset_id, id, &mut capabilities);
            Some(ProviderModelSettings {
                id: id.to_string(),
                source: "discovered".to_string(),
                status: "available".to_string(),
                enabled: false,
                allow_automatic_routing: false,
                tool_mode: source
                    .and_then(|item| item.get("tools"))
                    .and_then(|item| item.as_str())
                    .map_or("unknown", |value| {
                        if value == "no" {
                            "text_only"
                        } else {
                            "unknown"
                        }
                    })
                    .to_string(),
                capabilities,
                capability_states: Vec::new(),
                last_seen_at: Some(now.clone()),
                native_web_search_status: if profile.native_search.adapter_id == "none" {
                    "not_applicable".to_string()
                } else {
                    "declared".to_string()
                },
                native_web_search_error_code: None,
                native_web_search_verified_at: None,
                route_capabilities: route_capabilities_for_model(
                    &profile.invocation_routes,
                    if profile.native_search.adapter_id == "none" {
                        "not_applicable"
                    } else {
                        "declared"
                    },
                ),
                tool_contracts: Vec::new(),
            })
        })
        .collect();
    let resolved = result
        .get("resolved_api_base_url")
        .and_then(|value| value.as_str())
        .map(ToString::to_string);
    let settings = manager
        .replace_provider_catalog(&provider_id, discovered, resolved)
        .map_err(|error| error.to_string())?;
    if let Ok(mut health) = app.state::<Mutex<RouteHealthRegistry>>().lock() {
        let _ = health.clear_configuration_blocks(&provider_id);
    }
    if let Some(updated) = settings
        .models
        .providers
        .iter()
        .find(|item| item.id == provider_id)
    {
        inject_provider(&app, &core, &credentials, updated).await?;
    }
    Ok(settings)
}

#[derive(Deserialize)]
pub struct ModelAdmissionChange {
    pub provider_id: String,
    pub model_id: String,
    pub enabled: bool,
    #[serde(default)]
    pub confirm_affected_conversations: bool,
}

#[tauri::command]
pub async fn probe_model_tool_capability(
    app: AppHandle,
    manager: State<'_, SettingsManager>,
    core: State<'_, Mutex<CoreProcessManager>>,
    credentials: State<'_, CredentialStore>,
    provider_id: String,
    model_id: String,
) -> Result<AppSettings, String> {
    let settings = catalog_settings(&app, &core, &manager).await?;
    let profile = settings
        .models
        .providers
        .iter()
        .find(|item| item.id == provider_id)
        .ok_or_else(|| "The provider does not exist".to_string())?;
    let model = profile
        .models
        .iter()
        .find(|item| item.id == model_id && item.enabled)
        .ok_or_else(|| "Allow this model before testing its tool support".to_string())?;
    let _ = model;
    inject_provider(&app, &core, &credentials, profile).await?;
    let result = request_core_with_timeout(
        &app,
        &core,
        "model.tool_capability.probe",
        json!({"provider_profile_id": provider_id, "model_id": model_id}),
        std::time::Duration::from_secs(130),
    )
    .await?;
    if provider_id.starts_with("managed:") {
        return catalog_settings(&app, &core, &manager).await;
    }
    let mode = result
        .get("tool_mode")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| "The model returned an invalid tool capability result".to_string())?;
    let updated = manager
        .update_model_tool_mode(&provider_id, &model_id, mode)
        .map_err(|error| error.to_string())?;
    let profile = updated
        .models
        .providers
        .iter()
        .find(|profile| profile.id == provider_id)
        .ok_or_else(|| "The provider profile does not exist".to_string())?;
    inject_provider(&app, &core, &credentials, profile).await?;
    Ok(updated)
}

#[tauri::command]
pub async fn set_provider_model_enabled(
    app: AppHandle,
    manager: State<'_, SettingsManager>,
    core: State<'_, Mutex<CoreProcessManager>>,
    credentials: State<'_, CredentialStore>,
    change: ModelAdmissionChange,
) -> Result<AppSettings, String> {
    if !change.enabled {
        let affected = count_conversation_model_references(
            app.clone(),
            core.clone(),
            change.provider_id.clone(),
            change.model_id.clone(),
        )
        .await?;
        if affected > 0 && !change.confirm_affected_conversations {
            return Err(format!("confirmation_required:{affected}"));
        }
    }
    let settings = manager
        .set_provider_model_enabled(&change.provider_id, &change.model_id, change.enabled)
        .map_err(|error| error.to_string())?;
    let profile = settings
        .models
        .providers
        .iter()
        .find(|item| item.id == change.provider_id)
        .ok_or_else(|| "The provider profile does not exist".to_string())?;
    inject_provider(&app, &core, &credentials, profile).await?;
    Ok(settings)
}

#[derive(Debug, Deserialize)]
pub struct ModelAutomaticRoutingChange {
    provider_id: String,
    model_id: String,
    allowed: bool,
    #[serde(default)]
    confirm_affected_conversations: bool,
}

#[tauri::command]
pub async fn set_model_automatic_routing(
    app: AppHandle,
    manager: State<'_, SettingsManager>,
    core: State<'_, Mutex<CoreProcessManager>>,
    credentials: State<'_, CredentialStore>,
    change: ModelAutomaticRoutingChange,
) -> Result<AppSettings, String> {
    if !change.allowed {
        let affected = count_conversation_model_references(
            app.clone(),
            core.clone(),
            change.provider_id.clone(),
            change.model_id.clone(),
        )
        .await?;
        if affected > 0 && !change.confirm_affected_conversations {
            return Err(format!("confirmation_required:{affected}"));
        }
    }
    let settings = manager
        .set_model_automatic_routing(&change.provider_id, &change.model_id, change.allowed)
        .map_err(|error| error.to_string())?;
    let profile = settings
        .models
        .providers
        .iter()
        .find(|profile| profile.id == change.provider_id)
        .ok_or_else(|| "The provider profile does not exist".to_string())?;
    inject_provider(&app, &core, &credentials, profile).await?;
    Ok(settings)
}

#[tauri::command]
pub async fn count_conversation_model_references(
    app: AppHandle,
    core: State<'_, Mutex<CoreProcessManager>>,
    provider_id: String,
    model_id: String,
) -> Result<usize, String> {
    let result = request_core(
        &app,
        &core,
        "conversation.list",
        json!({"kind": null, "chat_group_id": null, "project_id": null}),
    )
    .await?;
    let conversations = result
        .get("conversations")
        .and_then(|value| value.as_array())
        .ok_or_else(|| "Core returned an invalid conversation list".to_string())?;
    Ok(conversations
        .iter()
        .filter(|conversation| {
            conversation
                .get("provider_profile_id")
                .and_then(|value| value.as_str())
                == Some(&provider_id)
                && conversation
                    .get("model_id")
                    .and_then(|value| value.as_str())
                    == Some(&model_id)
        })
        .count())
}

#[tauri::command]
pub async fn count_provider_conversation_references(
    app: AppHandle,
    core: State<'_, Mutex<CoreProcessManager>>,
    provider_id: String,
) -> Result<usize, String> {
    let result = request_core(
        &app,
        &core,
        "conversation.list",
        json!({"kind": null, "chat_group_id": null, "project_id": null}),
    )
    .await?;
    let conversations = result
        .get("conversations")
        .and_then(|value| value.as_array())
        .ok_or_else(|| "Core returned an invalid conversation list".to_string())?;
    Ok(conversations
        .iter()
        .filter(|conversation| {
            conversation
                .get("provider_profile_id")
                .and_then(|value| value.as_str())
                == Some(&provider_id)
        })
        .count())
}

#[tauri::command]
pub fn add_manual_provider_model(
    manager: State<'_, SettingsManager>,
    provider_id: String,
    model_id: String,
) -> Result<AppSettings, String> {
    manager
        .add_manual_provider_model(&provider_id, model_id)
        .map_err(|error| error.to_string())
}

#[derive(Debug, Deserialize)]
pub struct ModelContextLimitsRequest {
    provider_id: String,
    model_id: String,
    context_length: Option<u64>,
    max_output_tokens: Option<u64>,
}

#[tauri::command]
pub async fn update_model_context_limits(
    app: AppHandle,
    manager: State<'_, SettingsManager>,
    core: State<'_, Mutex<CoreProcessManager>>,
    credentials: State<'_, CredentialStore>,
    request: ModelContextLimitsRequest,
) -> Result<AppSettings, String> {
    let ModelContextLimitsRequest {
        provider_id,
        model_id,
        context_length,
        max_output_tokens,
    } = request;
    let settings = manager
        .update_model_context_limits(&provider_id, &model_id, context_length, max_output_tokens)
        .map_err(|error| error.to_string())?;
    let profile = settings
        .models
        .providers
        .iter()
        .find(|profile| profile.id == provider_id)
        .ok_or_else(|| "The provider profile does not exist".to_string())?;
    inject_provider(&app, &core, &credentials, profile).await?;
    Ok(settings)
}

#[derive(Debug, Deserialize)]
pub struct ModelRoutingMetadataRequest {
    provider_id: String,
    model_id: String,
    input_price: Option<String>,
    output_price: Option<String>,
    quality_score: Option<u8>,
    priority: i8,
}

#[tauri::command]
pub async fn update_model_routing_metadata(
    app: AppHandle,
    manager: State<'_, SettingsManager>,
    core: State<'_, Mutex<CoreProcessManager>>,
    credentials: State<'_, CredentialStore>,
    request: ModelRoutingMetadataRequest,
) -> Result<AppSettings, String> {
    let ModelRoutingMetadataRequest {
        provider_id,
        model_id,
        input_price,
        output_price,
        quality_score,
        priority,
    } = request;
    let settings = manager
        .update_model_routing_metadata(
            &provider_id,
            &model_id,
            input_price,
            output_price,
            quality_score,
            priority,
        )
        .map_err(|error| error.to_string())?;
    let profile = settings
        .models
        .providers
        .iter()
        .find(|profile| profile.id == provider_id)
        .ok_or_else(|| "The provider profile does not exist".to_string())?;
    inject_provider(&app, &core, &credentials, profile).await?;
    Ok(settings)
}

#[tauri::command]
pub async fn update_model_vision_confirmation(
    app: AppHandle,
    manager: State<'_, SettingsManager>,
    core: State<'_, Mutex<CoreProcessManager>>,
    credentials: State<'_, CredentialStore>,
    provider_id: String,
    model_id: String,
    confirmed: Option<bool>,
) -> Result<AppSettings, String> {
    let settings = manager
        .update_model_vision_confirmation(&provider_id, &model_id, confirmed)
        .map_err(|error| error.to_string())?;
    let profile = settings
        .models
        .providers
        .iter()
        .find(|profile| profile.id == provider_id)
        .ok_or_else(|| "The provider profile does not exist".to_string())?;
    inject_provider(&app, &core, &credentials, profile).await?;
    Ok(settings)
}

#[tauri::command]
pub async fn remove_manual_provider_model(
    app: AppHandle,
    manager: State<'_, SettingsManager>,
    core: State<'_, Mutex<CoreProcessManager>>,
    credentials: State<'_, CredentialStore>,
    provider_id: String,
    model_id: String,
    confirm_affected_conversations: bool,
) -> Result<AppSettings, String> {
    let affected = count_conversation_model_references(
        app.clone(),
        core.clone(),
        provider_id.clone(),
        model_id.clone(),
    )
    .await?;
    if affected > 0 && !confirm_affected_conversations {
        return Err(format!("confirmation_required:{affected}"));
    }
    let settings = manager
        .remove_manual_provider_model(&provider_id, &model_id)
        .map_err(|error| error.to_string())?;
    let profile = settings
        .models
        .providers
        .iter()
        .find(|profile| profile.id == provider_id)
        .ok_or_else(|| "The provider profile does not exist".to_string())?;
    inject_provider(&app, &core, &credentials, profile).await?;
    Ok(settings)
}

#[tauri::command]
pub async fn set_model_provider_enabled(
    app: AppHandle,
    manager: State<'_, SettingsManager>,
    core: State<'_, Mutex<CoreProcessManager>>,
    credentials: State<'_, CredentialStore>,
    provider_id: String,
    enabled: bool,
    confirm_affected_conversations: bool,
) -> Result<AppSettings, String> {
    if !enabled {
        let affected =
            count_provider_conversation_references(app.clone(), core.clone(), provider_id.clone())
                .await?;
        if affected > 0 && !confirm_affected_conversations {
            return Err(format!("confirmation_required:{affected}"));
        }
    }
    let settings = manager
        .set_provider_enabled(&provider_id, enabled)
        .map_err(|error| error.to_string())?;
    if enabled {
        let profile = settings
            .models
            .providers
            .iter()
            .find(|item| item.id == provider_id)
            .ok_or_else(|| "The provider profile does not exist".to_string())?;
        inject_provider(&app, &core, &credentials, profile).await?;
    } else {
        let _ = request_core(
            &app,
            &core,
            "provider.clear",
            json!({"provider_profile_id": provider_id}),
        )
        .await;
    }
    Ok(settings)
}

#[tauri::command]
pub async fn delete_model_provider(
    app: AppHandle,
    manager: State<'_, SettingsManager>,
    core: State<'_, Mutex<CoreProcessManager>>,
    credentials: State<'_, CredentialStore>,
    provider_id: String,
    confirm_affected_conversations: bool,
) -> Result<AppSettings, String> {
    let affected =
        count_provider_conversation_references(app.clone(), core.clone(), provider_id.clone())
            .await?;
    if affected > 0 && !confirm_affected_conversations {
        return Err(format!("confirmation_required:{affected}"));
    }
    let previous = manager
        .get_settings()
        .map_err(|error| error.to_string())?
        .models
        .providers
        .into_iter()
        .find(|profile| profile.id == provider_id)
        .ok_or_else(|| "The provider profile does not exist".to_string())?;
    let settings = manager
        .remove_provider(&provider_id)
        .map_err(|error| error.to_string())?;
    if let Err(error) = credentials.delete(&provider_id) {
        let _ = manager.upsert_provider(previous);
        return Err(error);
    }
    let _ = request_core(
        &app,
        &core,
        "provider.clear",
        json!({"provider_profile_id": provider_id}),
    )
    .await;
    Ok(settings)
}

#[derive(Debug, Serialize)]
pub struct SettingsApplyResult {
    settings: AppSettings,
    core_sync: String,
    warning_code: Option<String>,
}

#[tauri::command]
pub async fn update_default_model(
    app: AppHandle,
    manager: State<'_, SettingsManager>,
    core: State<'_, Mutex<CoreProcessManager>>,
    credentials: State<'_, CredentialStore>,
    provider_profile_id: String,
    model_id: String,
) -> Result<SettingsApplyResult, String> {
    if provider_profile_id.starts_with("managed:") {
        let configured = catalog_settings(&app, &core, &manager).await?;
        if !configured.models.providers.iter().any(|profile| {
            profile.id == provider_profile_id
                && profile.models.iter().any(|model| model.id == model_id)
        }) {
            return Err("Managed model is no longer imported.".into());
        }
        request_core(&app, &core, "local_model.request", json!({"action": "model.configure", "params": {"model_id": model_id, "is_default": true}})).await?;
        manager
            .update_managed_default(&provider_profile_id, &model_id)
            .map_err(|error| error.to_string())?;
        let settings = catalog_settings(&app, &core, &manager).await?;
        request_core(
            &app,
            &core,
            "routing.configure",
            routing_configuration(&settings),
        )
        .await?;
        return Ok(SettingsApplyResult {
            settings,
            core_sync: "applied".into(),
            warning_code: None,
        });
    }
    let configured = catalog_settings(&app, &core, &manager).await?;
    if !configured.models.providers.iter().any(|profile| {
        profile.id == provider_profile_id
            && profile.enabled
            && profile
                .models
                .iter()
                .any(|model| model.id == model_id && model.enabled && model.status != "unavailable")
    }) {
        return Err("The selected default model is unavailable offline.".into());
    }
    let settings = manager
        .update_default_model(provider_profile_id, model_id)
        .map_err(|error| error.to_string())?;
    let profile = settings
        .models
        .providers
        .iter()
        .find(|profile| profile.id == settings.models.default_provider_profile_id);
    let sync_result = async {
        for managed in configured.models.providers.iter().filter(|profile| profile.id.starts_with("managed:")) {
            for model in &managed.models {
                request_core(&app, &core, "local_model.request", json!({"action": "model.configure", "params": {"model_id": model.id, "is_default": false}})).await?;
            }
        }
        let profile = profile.ok_or_else(|| "The default provider does not exist".to_string())?;
        inject_provider(&app, &core, &credentials, profile).await?;
        request_core(
            &app,
            &core,
            "routing.configure",
            routing_configuration(&settings),
        )
        .await?;
        Ok::<(), String>(())
    }
    .await;
    Ok(SettingsApplyResult {
        settings,
        core_sync: if sync_result.is_ok() {
            "applied"
        } else {
            "pending"
        }
        .to_string(),
        warning_code: sync_result.err().map(|_| "core_sync_failed".to_string()),
    })
}
#[cfg(test)]
mod managed_catalog_tests {
    use super::*;
    #[test]
    fn overlays_stable_managed_catalog_without_private_transport_and_disables_saved_remote() {
        let mut original = AppSettings::default();
        let mut remote = original.models.providers[0].clone();
        remote.id = "saved-remote".into();
        remote.kind = "openai_compatible".into();
        remote.entered_base_url = "https://api.example.com".into();
        remote.resolved_api_base_url = None;
        remote.enabled = true;
        original.models.providers.push(remote);
        let catalog = json!({"models":[{"id":"a".repeat(64), "provider_profile_id":format!("managed:{}", "a".repeat(64)), "name":"Local GGUF", "status":"imported", "is_default":true,"context_length":8192,"tool_mode":"unknown"}]});
        let settings = apply_catalog(original.clone(), &catalog).unwrap();
        assert!(
            !settings
                .models
                .providers
                .iter()
                .find(|profile| profile.id == "saved-remote")
                .unwrap()
                .enabled
        );
        let managed = settings
            .models
            .providers
            .iter()
            .find(|profile| profile.id.starts_with("managed:"))
            .unwrap();
        assert_eq!(managed.models[0].status, "unavailable");
        assert!(managed.entered_base_url.is_empty());
        assert!(managed.credential_ref.is_none());
        assert!(managed.invocation_routes.is_empty());
        assert_eq!(settings.models.default_model_id, "a".repeat(64));
        assert!(!original
            .models
            .providers
            .iter()
            .any(|profile| profile.id.starts_with("managed:")));
        let mut loaded = catalog.clone();
        loaded["models"][0]["status"] = json!("loaded");
        assert_eq!(
            apply_catalog(original, &loaded)
                .unwrap()
                .models
                .providers
                .last()
                .unwrap()
                .models[0]
                .status,
            "available"
        );
    }
    #[test]
    fn rejects_spoofed_managed_profile_and_remote_configuration() {
        assert!(apply_catalog(
            AppSettings::default(),
            &json!({"models":[{"id":"a".repeat(64), "provider_profile_id":"managed:spoof"}]})
        )
        .is_err());
        assert!(validate_provider_url("ollama", "https://remote.test").is_err());
        assert!(validate_provider_url("openai_compatible", "http://127.0.0.1:8080/v1").is_ok());
    }
}
