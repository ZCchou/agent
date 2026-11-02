use std::{sync::Mutex, time::Duration};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, State};

use crate::{
    core_bridge::request_core_with_timeout,
    core_process_manager::CoreProcessManager,
    credentials::CredentialStore,
    route_health::{RouteHealthKey, RouteHealthRegistry},
    settings::{AppSettings, SearchBackendSettings, SettingsManager, WebSearchSettings},
    web_search::{domain::SearchBackendStatus, WebSearchService},
};

#[derive(Debug, Deserialize)]
pub struct SearchBackendInput {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub base_url: String,
    pub enabled: bool,
    pub api_key: Option<String>,
}

#[tauri::command]
pub fn update_web_search_settings(
    settings: WebSearchSettings,
    manager: State<'_, SettingsManager>,
) -> Result<AppSettings, String> {
    manager
        .update_web_search_settings(settings)
        .map_err(|error| error.to_string())
}

#[tauri::command]
pub fn save_search_backend(
    app: AppHandle,
    backend: SearchBackendInput,
    manager: State<'_, SettingsManager>,
    credentials: State<'_, CredentialStore>,
) -> Result<AppSettings, String> {
    let backend_identifier = backend.id.clone();
    let credential_id = format!("web-search:{}", backend.id);
    let previous = credentials.get(&credential_id)?;
    if let Some(secret) = backend.api_key.as_deref() {
        if secret.trim().is_empty() {
            credentials.delete(&credential_id)?;
        } else {
            credentials.set(&credential_id, secret)?;
        }
    }
    let existing = manager
        .get_settings()
        .map_err(|error| error.to_string())?
        .web_search
        .backends
        .into_iter()
        .find(|item| item.id == backend.id);
    let existing_ref = existing
        .as_ref()
        .and_then(|item| item.credential_ref.clone());
    let normalized_base_url = backend.base_url.trim_end_matches('/').to_string();
    let preserve_health = existing.as_ref().is_some_and(|item| {
        item.kind == backend.kind
            && item.base_url == normalized_base_url
            && backend.api_key.is_none()
    });
    let (last_status, last_tested_at) = if preserve_health {
        (
            existing.as_ref().and_then(|item| item.last_status.clone()),
            existing
                .as_ref()
                .and_then(|item| item.last_tested_at.clone()),
        )
    } else {
        (None, None)
    };
    let record = SearchBackendSettings {
        id: backend.id,
        name: backend.name,
        kind: backend.kind,
        base_url: normalized_base_url,
        enabled: backend.enabled,
        credential_ref: match backend.api_key.as_deref() {
            Some(secret) if secret.trim().is_empty() => None,
            Some(_) => Some("windows-credential-manager".to_string()),
            None => existing_ref,
        },
        last_status,
        last_tested_at,
    };
    match manager.upsert_search_backend(record) {
        Ok(settings) => {
            if !preserve_health {
                if let Ok(mut health) = app.state::<Mutex<RouteHealthRegistry>>().lock() {
                    let _ = health.clear_target("host_search", &backend_identifier);
                }
            }
            Ok(settings)
        }
        Err(error) => {
            match previous {
                Some(secret) => {
                    let _ = credentials.set(&credential_id, &secret);
                }
                None => {
                    let _ = credentials.delete(&credential_id);
                }
            }
            Err(error.to_string())
        }
    }
}

#[tauri::command]
pub fn delete_search_backend(
    app: AppHandle,
    backend_id: String,
    manager: State<'_, SettingsManager>,
    credentials: State<'_, CredentialStore>,
) -> Result<AppSettings, String> {
    let credential_id = format!("web-search:{backend_id}");
    let previous = credentials.get(&credential_id)?;
    credentials.delete(&credential_id)?;
    match manager.remove_search_backend(&backend_id) {
        Ok(settings) => {
            if let Ok(mut health) = app.state::<Mutex<RouteHealthRegistry>>().lock() {
                let _ = health.clear_target("host_search", &backend_id);
            }
            Ok(settings)
        }
        Err(error) => {
            if let Some(secret) = previous {
                let _ = credentials.set(&credential_id, &secret);
            }
            Err(error.to_string())
        }
    }
}

#[tauri::command]
pub async fn test_search_backend(
    app: tauri::AppHandle,
    backend_id: String,
) -> Result<SearchBackendStatus, String> {
    let manager = app.state::<SettingsManager>();
    let credentials = app.state::<CredentialStore>();
    let service = app.state::<WebSearchService>();
    let backend = manager
        .get_settings()
        .map_err(|error| error.to_string())?
        .web_search
        .backends
        .into_iter()
        .find(|item| item.id == backend_id)
        .ok_or_else(|| {
            "search_backend_not_configured: Search backend does not exist".to_string()
        })?;
    let result = service.test(&backend, &credentials).await;
    let tested_at = match &result {
        Ok(status) => status.tested_at.clone(),
        Err(_) => chrono::Utc::now().to_rfc3339(),
    };
    let health = match &result {
        Ok(status) if status.available => "available".to_string(),
        Ok(_) => "unavailable".to_string(),
        Err(error) => error
            .split_once(':')
            .map(|(code, _)| code)
            .unwrap_or("unavailable")
            .trim()
            .replace(
                |character: char| !character.is_ascii_alphanumeric() && character != '_',
                "_",
            ),
    };
    manager
        .update_search_backend_status(&backend.id, &health, &tested_at)
        .map_err(|error| error.to_string())?;
    result
}

#[derive(Deserialize, Serialize)]
struct NativeSearchProbeResult {
    status: String,
    adapter: String,
    route_id: String,
    capability_signature: Vec<String>,
}

#[tauri::command]
pub async fn probe_native_web_search(
    app: tauri::AppHandle,
    provider_id: String,
    model_id: String,
    manager: State<'_, SettingsManager>,
    core: State<'_, Mutex<CoreProcessManager>>,
) -> Result<AppSettings, String> {
    let configured = manager.get_settings().map_err(|error| error.to_string())?;
    let provider = configured
        .models
        .providers
        .iter()
        .find(|provider| provider.id == provider_id)
        .ok_or_else(|| "provider_not_found: The provider does not exist.".to_string())?;
    let route = provider
        .invocation_routes
        .iter()
        .find(|route| {
            route
                .capabilities
                .iter()
                .any(|value| value == "native_web_search")
        })
        .ok_or_else(|| {
            "invocation_route_unavailable: No native-search route is configured.".to_string()
        })?;
    let model = provider
        .models
        .iter()
        .find(|model| model.id == model_id && model.enabled)
        .ok_or_else(|| "model_not_enabled: Allow this model before diagnosis.".to_string())?;
    let route_id = route.route_id.clone();
    let capability_signature = model
        .route_capabilities
        .iter()
        .filter(|state| {
            state.route_id == route_id
                && state.route_config_fingerprint == route.config_fingerprint
                && state
                    .capability_signature
                    .iter()
                    .any(|value| value == "native_web_search")
        })
        .min_by_key(|state| state.capability_signature.len())
        .map(|state| state.capability_signature.clone())
        .ok_or_else(|| {
            "capability_signature_unknown: No native-search capability signature is configured."
                .to_string()
        })?;
    let adapter_id = route.adapter_id.clone();
    let result = match request_core_with_timeout(
        &app,
        &core,
        "provider.native_search.probe",
        serde_json::json!({
            "provider_profile_id": &provider_id,
            "model_id": &model_id,
            "route_id": &route_id,
            "capability_signature": &capability_signature,
        }),
        Duration::from_secs(100),
    )
    .await
    {
        Ok(result) => result,
        Err(error) => {
            let code = error
                .split(':')
                .next()
                .unwrap_or("native_search_diagnostic_failed");
            if code == "native_search_protocol_unsupported" {
                let _ = manager.update_model_route_capability_status(
                    &provider_id,
                    &model_id,
                    &route_id,
                    &capability_signature,
                    "unsupported",
                    Some(code),
                );
            } else if let Ok(mut registry) = app.state::<Mutex<RouteHealthRegistry>>().lock() {
                let _ = registry.record_failure(
                    RouteHealthKey {
                        target_kind: "model".to_string(),
                        provider_or_backend_id: provider_id.clone(),
                        model_id: Some(model_id.clone()),
                        route_id: route_id.clone(),
                        capability_signature: capability_signature.clone(),
                        route_fingerprint: route.config_fingerprint.clone(),
                        tool_transport: "none".to_string(),
                        tool_contract_version: String::new(),
                        tool_schema_hash: String::new(),
                    },
                    code,
                    None,
                    None,
                );
            }
            let _ = crate::settings::commands::reinject_providers(&app).await;
            return Err(error);
        }
    };
    let probe: NativeSearchProbeResult = serde_json::from_value(result)
        .map_err(|_| "invalid_core_response: Native search probe result is invalid".to_string())?;
    let status = match probe.status.as_str() {
        "supported" => "verified",
        "unsupported" => "unsupported",
        _ => {
            return Err("invalid_core_response: Native search probe status is invalid".to_string())
        }
    };
    if probe.route_id != route_id
        || probe.capability_signature != capability_signature
        || probe.adapter != adapter_id
    {
        return Err("invalid_core_response: Native search probe route is invalid".to_string());
    }
    let settings = manager
        .update_model_route_capability_status(
            &provider_id,
            &model_id,
            &route_id,
            &capability_signature,
            status,
            None,
        )
        .map_err(|error| error.to_string())?;
    if let Ok(mut registry) = app.state::<Mutex<RouteHealthRegistry>>().lock() {
        let _ = registry.record_success(RouteHealthKey {
            target_kind: "model".to_string(),
            provider_or_backend_id: provider_id.clone(),
            model_id: Some(model_id.clone()),
            route_id: route_id.clone(),
            capability_signature: capability_signature.clone(),
            route_fingerprint: route.config_fingerprint.clone(),
            tool_transport: "none".to_string(),
            tool_contract_version: String::new(),
            tool_schema_hash: String::new(),
        });
    }
    crate::settings::commands::reinject_providers(&app).await?;
    Ok(settings)
}
