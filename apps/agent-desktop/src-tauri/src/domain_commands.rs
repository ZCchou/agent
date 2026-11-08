use std::path::PathBuf;
use std::sync::Mutex;

use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, Manager, State};

use crate::{
    agent_execution::{AgentExecutionManager, ChangeSetView},
    attachment_store::{
        AttachmentImportBatchResult, AttachmentManifest, AttachmentNormalizationStatus,
        AttachmentPreview, AttachmentStore, RejectedAttachmentDescriptor,
    },
    core_bridge::request_core,
    core_process_manager::CoreProcessManager,
    route_health::RouteHealthRegistry,
    settings::SettingsManager,
    work_tools::{ApprovalMode, ProjectGrant, WorkPermissionView, WorkToolManager},
};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatGroup {
    pub id: String,
    pub name: String,
    pub sort_order: i64,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Project {
    pub id: String,
    pub name: String,
    pub category: String,
    pub sort_order: i64,
    pub created_at: String,
    pub updated_at: String,
    pub deleted_at: Option<String>,
    pub model_access_policy: String,
    pub model_scope_policy: String,
    pub web_access_policy: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Message {
    pub id: String,
    pub conversation_id: String,
    pub role: String,
    pub content: String,
    pub event_type: Option<String>,
    pub source_message_id: Option<String>,
    pub created_at: String,
    #[serde(default)]
    pub attachments: Vec<AttachmentManifest>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Conversation {
    pub id: String,
    pub kind: String,
    pub title: String,
    pub category: Option<String>,
    pub resolved_category: String,
    pub chat_group_id: Option<String>,
    pub project_id: Option<String>,
    pub sort_order: i64,
    pub derived_from_conversation_id: Option<String>,
    pub derived_through_message_id: Option<String>,
    pub derived_at: Option<String>,
    pub copied_from_conversation_id: Option<String>,
    pub copied_through_message_id: Option<String>,
    pub copied_at: Option<String>,
    pub created_at: String,
    pub updated_at: String,
    pub deleted_at: Option<String>,
    pub provider_profile_id: Option<String>,
    pub model_id: Option<String>,
    pub model_access_policy: Option<String>,
    pub model_scope_policy: Option<String>,
    pub purpose_override: Option<String>,
    pub web_search_mode: String,
    pub web_access_policy: Option<String>,
    #[serde(default = "fixed_selection_mode")]
    pub model_selection_mode: String,
    #[serde(default = "balanced_routing_preset")]
    pub routing_preset: String,
    #[serde(default)]
    pub coordinator_provider_profile_id: Option<String>,
    #[serde(default)]
    pub coordinator_model_id: Option<String>,
    #[serde(default)]
    pub messages: Vec<Message>,
}

fn fixed_selection_mode() -> String {
    "fixed".to_string()
}

fn balanced_routing_preset() -> String {
    "balanced".to_string()
}

#[derive(Deserialize)]
struct ChatGroupsResult {
    chat_groups: Vec<ChatGroup>,
}
#[derive(Deserialize)]
struct ChatGroupResult {
    chat_group: ChatGroup,
}
#[derive(Deserialize)]
struct ProjectsResult {
    projects: Vec<Project>,
}
#[derive(Deserialize)]
struct ProjectResult {
    project: Project,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectModelRef {
    pub provider_profile_id: String,
    pub model_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectModelScope {
    pub project_id: String,
    pub policy: String,
    pub models: Vec<ProjectModelRef>,
}

#[derive(Deserialize)]
struct ProjectModelScopeResult {
    scope: ProjectModelScope,
}
#[derive(Deserialize)]
struct ConversationsResult {
    conversations: Vec<Conversation>,
}
#[derive(Deserialize)]
struct ConversationResult {
    conversation: Conversation,
}

#[derive(Deserialize)]
struct AttachmentResult {
    attachment: PersistedAttachment,
}

#[derive(Deserialize)]
struct PersistedAttachment {
    #[serde(flatten)]
    manifest: AttachmentManifest,
    conversation_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TrashItem {
    pub entity_type: String,
    pub id: String,
    pub title: String,
    pub deleted_at: String,
    pub expires_at: String,
    pub work_count: u64,
    pub message_count: u64,
    pub conversation_kind: Option<String>,
    pub project_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectDeleteImpact {
    pub project_id: String,
    pub title: String,
    pub work_count: u64,
    pub message_count: u64,
}

#[derive(Deserialize)]
struct TrashItemResult {
    trash_item: TrashItem,
}
#[derive(Deserialize)]
struct TrashListResult {
    items: Vec<TrashItem>,
}
#[derive(Deserialize)]
struct TrashMutationResult {
    entity_type: String,
    id: String,
    #[serde(default)]
    storage_keys: Vec<String>,
}
#[derive(Deserialize)]
struct PurgeResult {
    project_ids: Vec<String>,
    conversation_ids: Vec<String>,
    #[serde(default)]
    storage_keys: Vec<String>,
}

fn decode<T: DeserializeOwned>(value: Value) -> Result<T, String> {
    serde_json::from_value(value).map_err(|_| "Agent Core returned an invalid result".to_string())
}

async fn core<T: DeserializeOwned>(
    app: &AppHandle,
    manager: &State<'_, Mutex<CoreProcessManager>>,
    message_type: &str,
    params: Value,
) -> Result<T, String> {
    decode(request_core(app, manager, message_type, params).await?)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelSelectionInput {
    pub mode: String,
    pub provider_profile_id: String,
    pub model_id: String,
    pub source: Option<String>,
    #[serde(default)]
    pub routing_policy: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AttachmentReference {
    pub attachment_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_start: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_end: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelRunStartRequest {
    pub conversation_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub edit_message_id: Option<String>,
    pub kind: Option<String>,
    pub category: Option<String>,
    pub chat_group_id: Option<String>,
    pub project_id: Option<String>,
    pub content: String,
    pub provider_profile_id: String,
    pub model_id: String,
    pub idempotency_key: Option<String>,
    #[serde(default, skip_serializing)]
    pub attachment_staging_ids: Vec<String>,
    #[serde(default)]
    pub attachment_references: Vec<AttachmentReference>,
    #[serde(default, skip_serializing)]
    pub attachment_rejection_staging_ids: Vec<String>,
    #[serde(default, skip_deserializing)]
    pub attachments: Vec<AttachmentManifest>,
    #[serde(default, skip_deserializing)]
    pub attachment_rejections: Vec<RejectedAttachmentDescriptor>,
    #[serde(default)]
    pub selection_source: Option<String>,
    #[serde(default)]
    pub selection: Option<ModelSelectionInput>,
    #[serde(default)]
    pub web_search_request_mode: Option<String>,
    #[serde(default, skip_deserializing)]
    pub web_search_global_enabled: bool,
    #[serde(default, skip_deserializing)]
    pub web_search_backend_id: Option<String>,
    #[serde(default, skip_deserializing)]
    pub web_search_backend_kind: Option<String>,
    #[serde(default, skip_deserializing)]
    pub web_search_backend_health_status: Option<String>,
    #[serde(default, skip_deserializing)]
    pub web_search_backend_retry_at: Option<String>,
}

fn effective_automatic_selection(persisted_mode: Option<&str>, requested_automatic: bool) -> bool {
    persisted_mode
        .map(|mode| mode == "automatic")
        .unwrap_or(requested_automatic)
}

fn is_local_model_profile(profile: &crate::settings::model::ProviderProfileSettings) -> bool {
    crate::offline::validate_profile(profile).is_ok()
}

#[tauri::command]
pub async fn start_model_run(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    settings: State<'_, SettingsManager>,
    attachment_store: State<'_, Mutex<AttachmentStore>>,
    mut request: ModelRunStartRequest,
) -> Result<Value, String> {
    if let Some(idempotency_key) = request.idempotency_key.as_ref() {
        let existing = request_core(
            &app,
            &manager,
            "model.run.find",
            json!({"idempotency_key": idempotency_key}),
        )
        .await?;
        if !existing.get("run").unwrap_or(&Value::Null).is_null() {
            return request_core(&app, &manager, "model.run.start", json!(request)).await;
        }
    }
    let requested_automatic = request
        .selection
        .as_ref()
        .is_some_and(|selection| selection.mode == "automatic");
    let existing_conversation = if let Some(conversation_id) = request.conversation_id.as_ref() {
        Some(
            core::<ConversationResult>(
                &app,
                &manager,
                "conversation.get",
                json!({"conversation_id": conversation_id}),
            )
            .await?
            .conversation,
        )
    } else {
        None
    };
    let automatic = effective_automatic_selection(
        existing_conversation
            .as_ref()
            .map(|conversation| conversation.model_selection_mode.as_str()),
        requested_automatic,
    );
    if let Some(selection) = request.selection.as_ref() {
        if !matches!(selection.mode.as_str(), "fixed" | "automatic") {
            return Err("invalid_model_selection: Unsupported selection mode.".to_string());
        }
        if existing_conversation.is_none() || automatic {
            request.provider_profile_id = selection.provider_profile_id.clone();
            request.model_id = selection.model_id.clone();
            request.selection_source = selection.source.clone();
        }
    }
    if let Some(conversation) = existing_conversation
        .as_ref()
        .filter(|conversation| conversation.model_selection_mode == "fixed")
    {
        request.provider_profile_id =
            conversation.provider_profile_id.clone().ok_or_else(|| {
                "invalid_model_selection: The fixed conversation has no provider.".to_string()
            })?;
        request.model_id = conversation.model_id.clone().ok_or_else(|| {
            "invalid_model_selection: The fixed conversation has no model.".to_string()
        })?;
        request.selection_source = Some("conversation_override".to_string());
    }
    if automatic {
        crate::settings::commands::reinject_providers(&app).await?;
    }
    let configured = crate::settings::commands::catalog_settings(&app, &manager, &settings).await?;
    request.web_search_global_enabled =
        configured.web_search.enabled && configured.web_search.privacy_acknowledged;
    if automatic {
        let requested_is_usable = configured.models.providers.iter().any(|profile| {
            profile.id == request.provider_profile_id
                && profile.enabled
                && profile.models.iter().any(|model| {
                    model.id == request.model_id
                        && model.enabled
                        && model.allow_automatic_routing
                        && model.status != "unavailable"
                        && model.capabilities.text != "no"
                })
        });
        if !requested_is_usable {
            let preferred = configured.models.providers.iter().find_map(|profile| {
                if !profile.enabled || profile.id != configured.models.default_provider_profile_id {
                    return None;
                }
                profile
                    .models
                    .iter()
                    .find(|model| {
                        model.id == configured.models.default_model_id
                            && model.enabled
                            && model.allow_automatic_routing
                            && model.status != "unavailable"
                            && model.capabilities.text != "no"
                    })
                    .map(|model| (profile.id.clone(), model.id.clone()))
            });
            let fallback = || {
                configured.models.providers.iter().find_map(|profile| {
                    if !profile.enabled {
                        return None;
                    }
                    profile
                        .models
                        .iter()
                        .find(|model| {
                            model.enabled
                                && model.allow_automatic_routing
                                && model.status != "unavailable"
                                && model.capabilities.text != "no"
                        })
                        .map(|model| (profile.id.clone(), model.id.clone()))
                })
            };
            let (provider_id, model_id) = preferred.or_else(fallback).ok_or_else(|| {
                "no_compatible_execution_plan: No enabled text model is available.".to_string()
            })?;
            request.provider_profile_id = provider_id;
            request.model_id = model_id;
        }
    }
    request.web_search_backend_id = configured.web_search.default_backend_id.clone();
    request.web_search_backend_kind = configured
        .web_search
        .default_backend_id
        .as_ref()
        .and_then(|backend_id| {
            configured
                .web_search
                .backends
                .iter()
                .find(|backend| &backend.id == backend_id && backend.enabled)
        })
        .map(|backend| backend.kind.clone());
    if let Some(backend_id) = request.web_search_backend_id.as_deref() {
        if let Ok(health) = app.state::<Mutex<RouteHealthRegistry>>().lock() {
            if let Some(record) = health.snapshot().into_iter().find(|record| {
                record.key.target_kind == "host_search"
                    && record.key.provider_or_backend_id == backend_id
                    && record.health_status != "ready"
            }) {
                request.web_search_backend_health_status = Some(record.health_status);
                request.web_search_backend_retry_at = record.retry_at;
            }
        }
    }
    let profile = configured
        .models
        .providers
        .iter()
        .find(|profile| profile.id == request.provider_profile_id && profile.enabled)
        .ok_or_else(|| "The selected provider is not enabled".to_string())?;
    let selected_catalog_model = profile
        .models
        .iter()
        .find(|model| model.id == request.model_id && model.enabled)
        .ok_or_else(|| "model_not_enabled: The selected model is not allowed.".to_string())?;
    if selected_catalog_model.status == "unavailable" {
        return Err(
            "model_unavailable: The selected model is no longer reported by its provider."
                .to_string(),
        );
    }
    if selected_catalog_model.capabilities.text == "no" {
        return Err(
            "model_capability_unsupported: The selected model cannot generate text.".to_string(),
        );
    }
    let attachments = attachment_store
        .lock()
        .map_err(|_| "attachment_store_unavailable: Attachment storage is busy.".to_string())?
        .resolve(&request.attachment_staging_ids)?;
    let attachment_rejections = attachment_store
        .lock()
        .map_err(|_| "attachment_store_unavailable: Attachment storage is busy.".to_string())?
        .resolve_rejections(&request.attachment_rejection_staging_ids)?;
    // Core plans from the current capability/route state, preserving fixed
    // selection and accepting a text fallback when images cannot be read.
    // Admission still requires trusted, normalized attachment snapshots.
    if attachments
        .iter()
        .filter(|attachment| attachment.kind == "image")
        .any(|attachment| {
            attachment.model_storage_key.is_none()
                && attachment.normalization_version.as_deref() != Some("legacy_passthrough")
        })
    {
        return Err(
            "image_normalization_failed: The image has no safe model-ready copy.".to_string(),
        );
    }
    request.attachments = attachments;
    request.attachment_rejections = attachment_rejections;
    let target_project_id = existing_conversation
        .as_ref()
        .and_then(|conversation| conversation.project_id.clone())
        .or_else(|| request.project_id.clone());
    request.web_search_request_mode = Some("off".into());
    let configured_web_mode = request
        .web_search_request_mode
        .as_deref()
        .unwrap_or_else(|| {
            existing_conversation
                .as_ref()
                .map(|conversation| conversation.web_search_mode.as_str())
                .unwrap_or("inherit")
        });
    let effective_web_mode = if configured_web_mode == "inherit" {
        if configured.web_search.enabled && configured.web_search.privacy_acknowledged {
            "auto"
        } else {
            "off"
        }
    } else {
        configured_web_mode
    };
    // AUTO is permission to search, not a requirement to search. Core resolves
    // the current message intent before enforcing consent or route availability.
    // Host performs the eager gate only for the explicit one-shot request.
    if effective_web_mode == "force_once" {
        if !request.web_search_global_enabled {
            return Err(
                "web_privacy_consent_required: Confirm web-search privacy before searching."
                    .to_string(),
            );
        }
        let project_web_allowed = if let Some(conversation) = existing_conversation.as_ref() {
            conversation.kind != "project_work"
                || conversation.web_access_policy.as_deref() == Some("allow_search")
        } else if let Some(project_id) = request.project_id.as_ref() {
            core::<ProjectsResult>(&app, &manager, "project.list", json!({}))
                .await?
                .projects
                .iter()
                .find(|project| project.id == *project_id)
                .is_some_and(|project| project.web_access_policy == "allow_search")
        } else {
            true
        };
        if !project_web_allowed {
            return Err(
                "project_web_consent_required: Allow web search for this project first."
                    .to_string(),
            );
        }
        let native_available = if automatic {
            configured
                .models
                .providers
                .iter()
                .filter(|candidate| candidate.enabled)
                .any(|candidate| {
                    candidate.native_search.adapter_id != "none"
                        && candidate.models.iter().any(|model| {
                            model.enabled
                                && model.allow_automatic_routing
                                && model.status != "unavailable"
                                && matches!(
                                    model.native_web_search_status.as_str(),
                                    "declared" | "verified"
                                )
                        })
                })
        } else {
            matches!(
                selected_catalog_model.native_web_search_status.as_str(),
                "declared" | "verified"
            ) && profile.native_search.adapter_id != "none"
        };
        let host_available = configured
            .web_search
            .default_backend_id
            .as_ref()
            .is_some_and(|backend_id| {
                configured
                    .web_search
                    .backends
                    .iter()
                    .any(|backend| backend.id == *backend_id && backend.enabled)
            });
        if !native_available && !host_available {
            return Err(
                "external_search_setup_required: No native or external search path is available."
                    .to_string(),
            );
        }
        if !automatic
            && !native_available
            && selected_catalog_model.tool_mode == "text_only"
            && effective_web_mode != "force_once"
        {
            return Err(
                "native_web_search_unsupported: Text-only models require Search this message."
                    .to_string(),
            );
        }
    }
    if !automatic {
        if let Some(project_id) = target_project_id.as_ref() {
            let scope = core::<ProjectModelScopeResult>(
                &app,
                &manager,
                "project.model_scope.get",
                json!({"project_id": project_id}),
            )
            .await?
            .scope;
            if scope.policy == "restricted"
                && !scope.models.iter().any(|candidate| {
                    candidate.provider_profile_id == request.provider_profile_id
                        && candidate.model_id == request.model_id
                })
            {
                return Err(
                    "model_not_enabled: The selected model is not allowed by this project."
                        .to_string(),
                );
            }
        }
    }
    let host_local = is_local_model_profile(profile);
    if !automatic && !host_local {
        let local_only = if let Some(conversation) = existing_conversation.as_ref() {
            conversation.model_access_policy.as_deref() == Some("local_only")
        } else if let Some(project_id) = request.project_id.as_ref() {
            core::<ProjectsResult>(&app, &manager, "project.list", json!({}))
                .await?
                .projects
                .iter()
                .any(|project| {
                    project.id == *project_id && project.model_access_policy == "local_only"
                })
        } else {
            false
        };
        if local_only {
            return Err(
                "online_model_not_allowed: This project only allows local models.".to_string(),
            );
        }
    }
    if request.idempotency_key.is_none() {
        request.idempotency_key = Some(uuid::Uuid::new_v4().to_string());
    }
    if request.selection_source.is_none() {
        request.selection_source = Some(
            if existing_conversation.as_ref().is_some_and(|conversation| {
                conversation.provider_profile_id.as_deref() == Some(&request.provider_profile_id)
                    && conversation.model_id.as_deref() == Some(&request.model_id)
            }) {
                "conversation_override"
            } else if configured.models.default_provider_profile_id == request.provider_profile_id
                && configured.models.default_model_id == request.model_id
            {
                "global_default"
            } else {
                "user_once"
            }
            .to_string(),
        );
    }
    request.selection = Some(ModelSelectionInput {
        mode: if automatic { "automatic" } else { "fixed" }.to_string(),
        provider_profile_id: request.provider_profile_id.clone(),
        model_id: request.model_id.clone(),
        source: request.selection_source.clone(),
        routing_policy: if automatic {
            Some(json!({
                "preset": configured.routing.default_preset,
                "allow_online": true,
                "max_extra_online_cost_usd": configured.routing.max_extra_online_cost_usd,
            }))
        } else {
            None
        },
    });
    let staged_ids = request.attachment_staging_ids.clone();
    let rejected_ids = request.attachment_rejection_staging_ids.clone();
    let result = request_core(&app, &manager, "model.run.start", json!(request)).await?;
    if result.get("status").and_then(Value::as_str) == Some("preflight_rejected") {
        return Ok(result);
    }
    let mut store = attachment_store
        .lock()
        .map_err(|_| "attachment_store_unavailable: Attachment storage is busy.".to_string())?;
    store.mark_committed(&staged_ids);
    store.mark_rejections_committed(&rejected_ids);
    Ok(result)
}

#[tauri::command]
pub async fn resolve_model_run_budget(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    run_id: String,
    plan_hash: String,
    approved: bool,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "model.run.budget.resolve",
        json!({"run_id": run_id, "plan_hash": plan_hash, "approved": approved}),
    )
    .await
}

#[tauri::command]
pub async fn get_model_run_plan(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    plan_id: String,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "model.run.plan.get",
        json!({"plan_id": plan_id}),
    )
    .await
}

#[derive(Debug, Deserialize)]
pub struct ConversationRoutingUpdateRequest {
    conversation_id: String,
    mode: String,
    preset: Option<String>,
    provider_profile_id: Option<String>,
    model_id: Option<String>,
}

#[tauri::command]
pub async fn update_conversation_routing(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    settings: State<'_, SettingsManager>,
    request: ConversationRoutingUpdateRequest,
) -> Result<Value, String> {
    let ConversationRoutingUpdateRequest {
        conversation_id,
        mode,
        preset,
        provider_profile_id,
        model_id,
    } = request;
    if provider_profile_id.is_some() != model_id.is_some() {
        return Err(
            "invalid_model_selection: Provider and model must be changed together.".to_string(),
        );
    }
    if mode == "fixed" && provider_profile_id.is_none() {
        return Err(
            "invalid_model_selection: A fixed route requires a provider and model.".to_string(),
        );
    }
    if mode == "fixed" || provider_profile_id.is_some() {
        let provider_id = provider_profile_id.as_ref().ok_or_else(|| {
            "invalid_model_selection: A fixed route requires a provider and model.".to_string()
        })?;
        let selected_model_id = model_id.as_ref().ok_or_else(|| {
            "invalid_model_selection: A fixed route requires a provider and model.".to_string()
        })?;
        let configured =
            crate::settings::commands::catalog_settings(&app, &manager, &settings).await?;
        let profile = configured
            .models
            .providers
            .iter()
            .find(|profile| profile.id == *provider_id && profile.enabled)
            .ok_or_else(|| "provider_disabled: The selected provider is disabled.".to_string())?;
        let selected_model = profile.models.iter().find(|model| {
            model.id == *selected_model_id && model.enabled && model.status != "unavailable"
        });
        if selected_model.is_none() {
            return Err("model_not_enabled: The selected model is not enabled.".to_string());
        }
        if mode == "automatic" && !selected_model.is_some_and(|model| model.allow_automatic_routing)
        {
            return Err(
                "coordinator_model_not_allowed_for_automatic: The selected model is not allowed for automatic calls."
                    .to_string(),
            );
        }
        let conversation = core::<ConversationResult>(
            &app,
            &manager,
            "conversation.get",
            json!({"conversation_id": conversation_id}),
        )
        .await?
        .conversation;
        if let Some(project_id) = conversation.project_id.as_ref() {
            let projects =
                core::<ProjectsResult>(&app, &manager, "project.list", json!({})).await?;
            let project = projects
                .projects
                .iter()
                .find(|project| project.id == *project_id)
                .ok_or_else(|| "project_not_found: The project does not exist.".to_string())?;
            let local = is_local_model_profile(profile);
            if project.model_access_policy == "local_only" && !local {
                return Err(
                    "online_model_not_allowed: This project only allows local models.".to_string(),
                );
            }
            let scope = core::<ProjectModelScopeResult>(
                &app,
                &manager,
                "project.model_scope.get",
                json!({"project_id": project_id}),
            )
            .await?
            .scope;
            if scope.policy == "restricted"
                && !scope.models.iter().any(|candidate| {
                    candidate.provider_profile_id == *provider_id
                        && candidate.model_id == *selected_model_id
                })
            {
                return Err(
                    "model_not_enabled: The selected model is not allowed by this project."
                        .to_string(),
                );
            }
        }
    } else if mode != "automatic" {
        return Err("invalid_model_selection: The selection mode is not supported.".to_string());
    }
    request_core(
        &app,
        &manager,
        "conversation.routing.update",
        json!({
            "conversation_id": conversation_id,
            "mode": mode,
            "preset": preset,
            "provider_profile_id": provider_profile_id,
            "model_id": model_id,
        }),
    )
    .await
}

#[tauri::command]
pub async fn update_conversation_web_search(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    conversation_id: String,
    mode: String,
) -> Result<Value, String> {
    if mode != "off" {
        crate::offline::deny_network()?;
    }
    request_core(
        &app,
        &manager,
        "conversation.web_search.update",
        json!({
            "conversation_id": conversation_id, "mode": mode,
        }),
    )
    .await
}

#[tauri::command]
pub async fn update_project_web_access(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    project_id: String,
    policy: String,
) -> Result<Value, String> {
    if policy != "disabled" {
        crate::offline::deny_network()?;
    }
    request_core(
        &app,
        &manager,
        "project.web_access.update",
        json!({
            "project_id": project_id, "policy": policy,
        }),
    )
    .await
}

#[tauri::command]
pub async fn list_web_operations(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    run_id: String,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "web.operation.list",
        json!({"run_id": run_id}),
    )
    .await
}

#[tauri::command]
pub async fn list_conversation_web_operations(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    conversation_id: String,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "web.operation.list_conversation",
        json!({"conversation_id": conversation_id}),
    )
    .await
}

#[tauri::command]
pub async fn preview_web_source(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    run_id: String,
    source_id: String,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "web.source.preview",
        json!({
            "run_id": run_id, "source_id": source_id,
        }),
    )
    .await
}

#[tauri::command]
pub async fn select_message_attachments(
    attachment_store: State<'_, Mutex<AttachmentStore>>,
) -> Result<AttachmentImportBatchResult, String> {
    let paths = tauri::async_runtime::spawn_blocking(|| {
        rfd::FileDialog::new()
            .add_filter(
                "Supported attachments",
                &crate::attachment_processor::AttachmentProcessorRegistry::supported_extensions(),
            )
            .pick_files()
    })
    .await
    .map_err(|_| "attachment_picker_failed: File picker could not be opened.".to_string())?
    .unwrap_or_default();
    if paths.is_empty() {
        return Ok(AttachmentImportBatchResult {
            accepted: Vec::new(),
            rejected: Vec::new(),
        });
    }
    let result = attachment_store
        .lock()
        .map_err(|_| "attachment_store_unavailable: Attachment storage is busy.".to_string())?
        .import_paths(&paths);
    Ok(result)
}

#[tauri::command]
pub async fn import_clipboard_image(
    app: AppHandle,
    name: String,
    data_base64: String,
) -> Result<AttachmentImportBatchResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<Mutex<AttachmentStore>>();
        let mut store = state
            .lock()
            .map_err(|_| "attachment_store_unavailable: Attachment storage is busy.".to_string())?;
        Ok(store.import_clipboard_image(&name, &data_base64))
    })
    .await
    .map_err(|_| "attachment_processing_failed: Clipboard image import failed.".to_string())?
}

#[tauri::command]
pub fn import_dropped_attachments(
    attachment_store: State<'_, Mutex<AttachmentStore>>,
    paths: Vec<String>,
) -> Result<AttachmentImportBatchResult, String> {
    let paths = paths.into_iter().map(PathBuf::from).collect::<Vec<_>>();
    let result = attachment_store
        .lock()
        .map_err(|_| "attachment_store_unavailable: Attachment storage is busy.".to_string())?
        .import_paths(&paths);
    Ok(result)
}

#[tauri::command]
pub fn discard_staged_attachment(
    attachment_store: State<'_, Mutex<AttachmentStore>>,
    attachment_id: String,
) -> Result<(), String> {
    attachment_store
        .lock()
        .map_err(|_| "attachment_store_unavailable: Attachment storage is busy.".to_string())?
        .discard(&attachment_id)
}

#[tauri::command]
pub fn preview_staged_attachment(
    attachment_store: State<'_, Mutex<AttachmentStore>>,
    attachment_id: String,
) -> Result<AttachmentPreview, String> {
    attachment_store
        .lock()
        .map_err(|_| "attachment_store_unavailable: Attachment storage is busy.".to_string())?
        .preview(&attachment_id)
}

#[tauri::command]
pub async fn preview_message_attachment(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    attachment_store: State<'_, Mutex<AttachmentStore>>,
    conversation_id: String,
    attachment_id: String,
) -> Result<AttachmentPreview, String> {
    let persisted = core::<AttachmentResult>(
        &app,
        &manager,
        "attachment.get",
        json!({"attachment_id": attachment_id}),
    )
    .await?
    .attachment;
    if persisted.conversation_id != conversation_id {
        return Err(
            "attachment_scope_mismatch: Attachment does not belong to this conversation."
                .to_string(),
        );
    }
    attachment_store
        .lock()
        .map_err(|_| "attachment_store_unavailable: Attachment storage is busy.".to_string())?
        .preview_manifest(&persisted.manifest)
}

#[tauri::command]
pub async fn get_attachment_normalization_status(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    attachment_store: State<'_, Mutex<AttachmentStore>>,
    conversation_id: String,
    attachment_id: String,
) -> Result<AttachmentNormalizationStatus, String> {
    let persisted = core::<AttachmentResult>(
        &app,
        &manager,
        "attachment.get",
        json!({"attachment_id": attachment_id}),
    )
    .await?
    .attachment;
    if persisted.conversation_id != conversation_id {
        return Err(
            "attachment_scope_mismatch: Attachment does not belong to this conversation."
                .to_string(),
        );
    }
    Ok(attachment_store
        .lock()
        .map_err(|_| "attachment_store_unavailable: Attachment storage is busy.".to_string())?
        .normalization_status(&persisted.manifest))
}

#[derive(Debug, Clone, Serialize)]
pub struct ProjectFilePreview {
    pub path: String,
    pub kind: String,
    pub media_type: String,
    pub content: Option<String>,
    pub data_url: Option<String>,
}

#[tauri::command]
pub fn open_project_root_directory(
    work_tools: State<'_, Mutex<WorkToolManager>>,
    project_id: String,
) -> Result<(), String> {
    let scope = work_tools
        .lock()
        .map_err(|_| "project_grant_unavailable: Project authorization is busy.".to_string())?
        .authorized_access(&project_id, "")?;
    let policy = crate::file_access::FileAccessPolicy;
    let target = policy.open_read(&scope, &policy.validate_relative(".")?)?;
    tauri_plugin_opener::open_path(target.path(), None::<&str>)
        .map_err(|error| format!("project_directory_open_failed: {error}"))
}

#[tauri::command]
pub async fn open_project_directory(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    work_tools: State<'_, Mutex<WorkToolManager>>,
    conversation_id: String,
    path: String,
) -> Result<(), String> {
    let conversation = core::<ConversationResult>(
        &app,
        &manager,
        "conversation.get",
        json!({"conversation_id": conversation_id}),
    )
    .await?
    .conversation;
    if conversation.kind != "project_work" {
        return Err(
            "project_directory_not_allowed: Standard chats cannot open project directories."
                .to_string(),
        );
    }
    let project_id = conversation
        .project_id
        .ok_or_else(|| "project_directory_not_allowed: Work has no project.".to_string())?;
    let scope = work_tools
        .lock()
        .map_err(|_| "project_grant_unavailable: Project authorization is busy.".to_string())?
        .authorized_access(&project_id, &conversation_id)?;
    let policy = crate::file_access::FileAccessPolicy;
    let target = policy.open_read(&scope, &policy.validate_relative(&path)?)?;
    if !target.is_dir() {
        return Err(
            "project_directory_not_found: The requested path is not a directory.".to_string(),
        );
    }
    tauri_plugin_opener::open_path(target.path(), None::<&str>)
        .map_err(|error| format!("project_directory_open_failed: {error}"))
}

#[tauri::command]
pub async fn preview_project_file(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    work_tools: State<'_, Mutex<WorkToolManager>>,
    conversation_id: String,
    path: String,
) -> Result<ProjectFilePreview, String> {
    let conversation = core::<ConversationResult>(
        &app,
        &manager,
        "conversation.get",
        json!({"conversation_id": conversation_id}),
    )
    .await?
    .conversation;
    if conversation.kind != "project_work" {
        return Err(
            "project_file_not_allowed: Standard chats cannot open project files.".to_string(),
        );
    }
    let project_id = conversation
        .project_id
        .ok_or_else(|| "project_file_not_allowed: Work has no project.".to_string())?;
    let scope = work_tools
        .lock()
        .map_err(|_| "project_grant_unavailable: Project authorization is busy.".to_string())?
        .authorized_access(&project_id, &conversation_id)?;
    let policy = crate::file_access::FileAccessPolicy;
    let target = policy.open_read(&scope, &policy.validate_relative(&path)?)?;
    if !target.is_file() {
        return Err("project_file_not_found: The requested path is not a file.".to_string());
    }
    let extension = target
        .path()
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    let media_type = match extension.as_str() {
        "png" => Some("image/png"),
        "jpg" | "jpeg" => Some("image/jpeg"),
        "webp" => Some("image/webp"),
        "gif" => Some("image/gif"),
        _ => None,
    };
    if let Some(media_type) = media_type {
        if target.len() > 10 * 1024 * 1024 {
            return Err("project_file_too_large: Image preview exceeds 10 MiB.".to_string());
        }
        let bytes = target.read_bytes(10 * 1024 * 1024)?;
        return Ok(ProjectFilePreview {
            path,
            kind: "image".to_string(),
            media_type: media_type.to_string(),
            content: None,
            data_url: Some(format!(
                "data:{media_type};base64,{}",
                STANDARD.encode(bytes)
            )),
        });
    }
    if target.len() > 1024 * 1024 {
        return Err("project_file_too_large: Text preview exceeds 1 MiB.".to_string());
    }
    let content = String::from_utf8(target.read_bytes(1024 * 1024)?).map_err(|_| {
        "project_file_not_text: Only UTF-8 text and supported images can be previewed.".to_string()
    })?;
    Ok(ProjectFilePreview {
        path,
        kind: "text".to_string(),
        media_type: "text/plain".to_string(),
        content: Some(content),
        data_url: None,
    })
}

#[tauri::command]
pub async fn list_provider_models(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    provider_profile_id: String,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "provider.list_models",
        json!({"provider_profile_id": provider_profile_id}),
    )
    .await
}

#[cfg(test)]
mod routing_serialization_tests {
    use super::{
        effective_automatic_selection, AttachmentReference, Conversation, ModelRunStartRequest,
    };
    use crate::settings::invocation_routes_for_profile;
    use serde_json::json;

    #[test]
    fn selected_attachment_references_survive_host_serialization() {
        let request: ModelRunStartRequest = serde_json::from_value(json!({
            "content": "Read the selected section", "provider_profile_id": "local", "model_id": "text",
            "conversation_id": "conversation", "attachment_references": [
                {"attachment_id": "old-text", "source_start": 10, "source_end": 50}
            ], "attachments": [{"storage_key": "untrusted-client-path"}]
        })).unwrap();
        assert!(request.attachments.is_empty());
        let encoded = serde_json::to_value(request).unwrap();
        assert_eq!(
            encoded["attachment_references"],
            json!([
                {"attachment_id": "old-text", "source_start": 10, "source_end": 50}
            ])
        );
    }

    #[test]
    fn references_cannot_supply_a_path_or_capabilities() {
        for forged in [
            json!({"attachment_id": "old-text", "storage_key": "forged"}),
            json!({"attachment_id": "old-text", "sha256": "forged"}),
            json!({"attachment_id": "old-text", "source_start": -1}),
        ] {
            assert!(serde_json::from_value::<AttachmentReference>(forged).is_err());
        }
    }

    #[test]
    fn conversation_routing_fields_survive_the_host_boundary() {
        let value = json!({
            "id": "c1", "kind": "standard_chat", "title": "Chat", "category": "casual",
            "resolved_category": "casual", "chat_group_id": null, "project_id": null,
            "sort_order": 0, "derived_from_conversation_id": null,
            "derived_through_message_id": null, "derived_at": null,
            "copied_from_conversation_id": null, "copied_through_message_id": null,
            "copied_at": null, "created_at": "now", "updated_at": "now", "deleted_at": null,
            "provider_profile_id": null, "model_id": null, "model_access_policy": null,
            "model_scope_policy": null, "purpose_override": null, "web_search_mode": "inherit",
            "web_access_policy": null, "model_selection_mode": "automatic",
            "routing_preset": "quality", "coordinator_provider_profile_id": "p",
            "coordinator_model_id": "m", "messages": []
        });
        let conversation: Conversation = serde_json::from_value(value).unwrap();
        let encoded = serde_json::to_value(conversation).unwrap();
        assert_eq!(encoded["model_selection_mode"], "automatic");
        assert_eq!(encoded["routing_preset"], "quality");
        assert_eq!(encoded["coordinator_provider_profile_id"], "p");
        assert_eq!(encoded["coordinator_model_id"], "m");
    }

    #[test]
    fn edit_target_crosses_host_boundary_without_client_attachment_authority() {
        let request: ModelRunStartRequest = serde_json::from_value(json!({
            "conversation_id": "chat", "content": "Edited question", "provider_profile_id": "provider",
            "model_id": "model", "edit_message_id": "original-user",
            "attachments": [{"storage_key": "untrusted"}]
        }))
        .unwrap();
        let forwarded = serde_json::to_value(request).unwrap();
        assert_eq!(forwarded["edit_message_id"], "original-user");
        assert_eq!(forwarded["attachments"], json!([]));
    }

    #[test]
    fn persisted_selection_mode_overrides_a_stale_webview_request() {
        assert!(!effective_automatic_selection(Some("fixed"), true));
        assert!(effective_automatic_selection(Some("automatic"), false));
        assert!(effective_automatic_selection(None, true));
        assert!(!effective_automatic_selection(None, false));
    }

    #[test]
    fn shared_execution_contract_matches_host_versions() {
        let contract: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../contracts/unified-invocation-v3.4.json"
        ))
        .unwrap();
        assert_eq!(contract["ipc_version"], 18);
        assert_eq!(contract["settings_schema_version"], 18);
        assert_eq!(contract["core_schema_version"], 25);
        assert_eq!(contract["core_version"], env!("CARGO_PKG_VERSION"));
        assert_eq!(contract["build_id"], "schema25-local-runtime-v1");

        let expected = invocation_routes_for_profile("deepseek", "https://api.deepseek.com", None);
        let contract_adapters = contract["adapters"].as_array().unwrap();
        for route in expected {
            assert!(contract_adapters
                .iter()
                .any(|candidate| candidate == &route.adapter_id));
        }
    }
}

#[tauri::command]
pub async fn list_project_knowledge(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    project_id: String,
    include_archived: Option<bool>,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "knowledge.list",
        json!({"project_id": project_id, "include_archived": include_archived.unwrap_or(false)}),
    )
    .await
}

#[tauri::command]
pub async fn get_global_instructions(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
) -> Result<Value, String> {
    request_core(&app, &manager, "memory.global_instructions.get", json!({})).await
}

#[tauri::command]
pub async fn update_global_instructions(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    content: String,
    enabled: bool,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "memory.global_instructions.update",
        json!({"content": content, "enabled": enabled}),
    )
    .await
}

#[tauri::command]
pub async fn reset_global_instructions(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "memory.global_instructions.reset",
        json!({}),
    )
    .await
}

#[tauri::command]
pub async fn propose_project_knowledge(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    conversation_id: String,
    knowledge_type: String,
    title: String,
    content: String,
    source_message_id: Option<String>,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "knowledge.propose",
        json!({
            "conversation_id": conversation_id, "knowledge_type": knowledge_type,
            "title": title, "content": content, "source_message_id": source_message_id
        }),
    )
    .await
}

#[tauri::command]
pub async fn publish_project_knowledge(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    proposal_id: String,
    title: Option<String>,
    content: Option<String>,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "knowledge.publish",
        json!({
            "proposal_id": proposal_id, "title": title, "content": content
        }),
    )
    .await
}

#[tauri::command]
pub async fn reject_project_knowledge(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    proposal_id: String,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "knowledge.reject",
        json!({"proposal_id": proposal_id}),
    )
    .await
}

#[tauri::command]
pub async fn update_project_knowledge(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    knowledge_id: String,
    title: Option<String>,
    content: Option<String>,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "knowledge.update",
        json!({
            "knowledge_id": knowledge_id, "title": title, "content": content
        }),
    )
    .await
}

#[tauri::command]
pub async fn pin_project_knowledge(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    knowledge_id: String,
    pinned: bool,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "knowledge.pin",
        json!({
            "knowledge_id": knowledge_id, "pinned": pinned
        }),
    )
    .await
}

#[tauri::command]
pub async fn archive_project_knowledge(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    knowledge_id: String,
    archived: bool,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "knowledge.archive",
        json!({
            "knowledge_id": knowledge_id, "archived": archived
        }),
    )
    .await
}

#[tauri::command]
pub async fn update_conversation_model(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    settings: State<'_, SettingsManager>,
    conversation_id: String,
    provider_profile_id: Option<String>,
    model_id: Option<String>,
) -> Result<Conversation, String> {
    if let Some(profile_id) = provider_profile_id.as_ref() {
        let configured =
            crate::settings::commands::catalog_settings(&app, &manager, &settings).await?;
        let profile = configured
            .models
            .providers
            .iter()
            .find(|profile| profile.id == *profile_id && profile.enabled)
            .ok_or_else(|| "The selected provider is not enabled".to_string())?;
        let selected_model = model_id.as_ref().ok_or_else(|| {
            "invalid_model_selection: Provider and model must be selected together.".to_string()
        })?;
        if !profile.models.iter().any(|model| {
            model.id == *selected_model && model.enabled && model.status != "unavailable"
        }) {
            return Err("model_not_enabled: The selected model is not allowed.".to_string());
        }
        let conversation = core::<ConversationResult>(
            &app,
            &manager,
            "conversation.get",
            json!({"conversation_id": conversation_id}),
        )
        .await?
        .conversation;
        if let Some(project_id) = conversation.project_id.as_ref() {
            let scope = core::<ProjectModelScopeResult>(
                &app,
                &manager,
                "project.model_scope.get",
                json!({"project_id": project_id}),
            )
            .await?
            .scope;
            if scope.policy == "restricted"
                && !scope.models.iter().any(|candidate| {
                    candidate.provider_profile_id == *profile_id
                        && candidate.model_id == *selected_model
                })
            {
                return Err(
                    "model_not_enabled: The selected model is not allowed by this project."
                        .to_string(),
                );
            }
        }
        let local = is_local_model_profile(profile);
        if conversation.model_access_policy.as_deref() == Some("local_only") && !local {
            return Err(
                "online_model_not_allowed: This project only allows local models.".to_string(),
            );
        }
    }
    Ok(core::<ConversationResult>(
        &app,
        &manager,
        "conversation.routing.update",
        json!({
            "conversation_id": conversation_id,
            "mode": if provider_profile_id.is_some() { "fixed" } else { "automatic" },
            "preset": null,
            "provider_profile_id": provider_profile_id,
            "model_id": model_id
        }),
    )
    .await?
    .conversation)
}

#[tauri::command]
pub async fn update_project_model_policy(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    project_id: String,
    model_access_policy: String,
) -> Result<Project, String> {
    Ok(core::<ProjectResult>(
        &app,
        &manager,
        "project.model_policy.update",
        json!({
            "project_id": project_id, "model_access_policy": model_access_policy
        }),
    )
    .await?
    .project)
}

#[tauri::command]
pub async fn get_project_model_scope(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    project_id: String,
) -> Result<ProjectModelScope, String> {
    Ok(core::<ProjectModelScopeResult>(
        &app,
        &manager,
        "project.model_scope.get",
        json!({"project_id": project_id}),
    )
    .await?
    .scope)
}

#[tauri::command]
pub async fn update_project_model_scope(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    settings: State<'_, SettingsManager>,
    project_id: String,
    policy: String,
    models: Vec<ProjectModelRef>,
) -> Result<ProjectModelScope, String> {
    if policy == "restricted" {
        let configured =
            crate::settings::commands::catalog_settings(&app, &manager, &settings).await?;
        if models.iter().any(|candidate| {
            !configured.models.providers.iter().any(|provider| {
                provider.id == candidate.provider_profile_id
                    && provider.enabled
                    && provider.models.iter().any(|model| {
                        model.id == candidate.model_id
                            && model.enabled
                            && (model.status != "unavailable"
                                || provider.id.starts_with("managed:"))
                    })
            })
        }) {
            return Err(
                "model_not_enabled: A project cannot enable a globally disabled model.".to_string(),
            );
        }
        let project = core::<ProjectsResult>(&app, &manager, "project.list", json!({}))
            .await?
            .projects
            .into_iter()
            .find(|project| project.id == project_id)
            .ok_or_else(|| "The project does not exist".to_string())?;
        if project.model_access_policy == "local_only"
            && models.iter().any(|candidate| {
                configured
                    .models
                    .providers
                    .iter()
                    .find(|provider| provider.id == candidate.provider_profile_id)
                    .is_some_and(|provider| !is_local_model_profile(provider))
            })
        {
            return Err(
                "online_model_not_allowed: This project only allows local models.".to_string(),
            );
        }
    }
    Ok(core::<ProjectModelScopeResult>(
        &app,
        &manager,
        "project.model_scope.update",
        json!({"project_id": project_id, "policy": policy, "models": models}),
    )
    .await?
    .scope)
}

#[tauri::command]
pub async fn list_chat_groups(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
) -> Result<Vec<ChatGroup>, String> {
    Ok(
        core::<ChatGroupsResult>(&app, &manager, "chat_group.list", json!({}))
            .await?
            .chat_groups,
    )
}

#[tauri::command]
pub async fn create_chat_group(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    name: String,
) -> Result<ChatGroup, String> {
    Ok(
        core::<ChatGroupResult>(&app, &manager, "chat_group.create", json!({"name": name}))
            .await?
            .chat_group,
    )
}

#[tauri::command]
pub async fn rename_chat_group(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    chat_group_id: String,
    name: String,
) -> Result<ChatGroup, String> {
    Ok(core::<ChatGroupResult>(
        &app,
        &manager,
        "chat_group.rename",
        json!({"chat_group_id": chat_group_id, "name": name}),
    )
    .await?
    .chat_group)
}

#[tauri::command]
pub async fn delete_chat_group(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    chat_group_id: String,
) -> Result<(), String> {
    let _: Value = core(
        &app,
        &manager,
        "chat_group.delete",
        json!({"chat_group_id": chat_group_id}),
    )
    .await?;
    Ok(())
}

#[tauri::command]
pub async fn reorder_chat_groups(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    ordered_ids: Vec<String>,
) -> Result<Vec<ChatGroup>, String> {
    Ok(core::<ChatGroupsResult>(
        &app,
        &manager,
        "chat_group.reorder",
        json!({"ordered_ids": ordered_ids}),
    )
    .await?
    .chat_groups)
}

#[tauri::command]
pub async fn list_projects(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
) -> Result<Vec<Project>, String> {
    Ok(
        core::<ProjectsResult>(&app, &manager, "project.list", json!({}))
            .await?
            .projects,
    )
}

#[tauri::command]
pub async fn create_project(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    name: String,
    category: String,
) -> Result<Project, String> {
    Ok(core::<ProjectResult>(
        &app,
        &manager,
        "project.create",
        json!({"name": name, "category": category}),
    )
    .await?
    .project)
}

#[tauri::command]
pub async fn update_project(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    project_id: String,
    name: String,
    category: String,
) -> Result<Project, String> {
    Ok(core::<ProjectResult>(
        &app,
        &manager,
        "project.update",
        json!({"project_id": project_id, "name": name, "category": category}),
    )
    .await?
    .project)
}

#[tauri::command]
pub async fn reorder_projects(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    ordered_ids: Vec<String>,
) -> Result<Vec<Project>, String> {
    Ok(core::<ProjectsResult>(
        &app,
        &manager,
        "project.reorder",
        json!({"ordered_ids": ordered_ids}),
    )
    .await?
    .projects)
}

#[tauri::command]
pub async fn list_conversations(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    kind: Option<String>,
    chat_group_id: Option<String>,
    project_id: Option<String>,
) -> Result<Vec<Conversation>, String> {
    Ok(core::<ConversationsResult>(
        &app,
        &manager,
        "conversation.list",
        json!({"kind": kind, "chat_group_id": chat_group_id, "project_id": project_id}),
    )
    .await?
    .conversations)
}

#[tauri::command]
pub async fn get_conversation(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    conversation_id: String,
) -> Result<Conversation, String> {
    Ok(core::<ConversationResult>(
        &app,
        &manager,
        "conversation.get",
        json!({"conversation_id": conversation_id}),
    )
    .await?
    .conversation)
}

#[tauri::command]
pub async fn create_conversation(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    kind: String,
    title: String,
    category: Option<String>,
    chat_group_id: Option<String>,
    project_id: Option<String>,
) -> Result<Conversation, String> {
    Ok(core::<ConversationResult>(
        &app,
        &manager,
        "conversation.create",
        json!({
            "kind": kind, "title": title, "category": category,
            "chat_group_id": chat_group_id, "project_id": project_id
        }),
    )
    .await?
    .conversation)
}

#[tauri::command]
pub async fn update_conversation(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    conversation_id: String,
    title: String,
    category: Option<String>,
) -> Result<Conversation, String> {
    Ok(core::<ConversationResult>(
        &app,
        &manager,
        "conversation.update",
        json!({
            "conversation_id": conversation_id, "title": title,
            "category": category
        }),
    )
    .await?
    .conversation)
}

#[tauri::command]
pub async fn derive_work(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    source_conversation_id: String,
    project_id: String,
    title: Option<String>,
) -> Result<Conversation, String> {
    Ok(core::<ConversationResult>(
        &app,
        &manager,
        "conversation.derive_work",
        json!({
            "source_conversation_id": source_conversation_id,
            "project_id": project_id,
            "title": title
        }),
    )
    .await?
    .conversation)
}

#[tauri::command]
pub async fn reorder_conversations(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    kind: String,
    container_id: Option<String>,
    ordered_ids: Vec<String>,
) -> Result<Vec<Conversation>, String> {
    Ok(core::<ConversationsResult>(
        &app,
        &manager,
        "conversation.reorder",
        json!({"kind": kind, "container_id": container_id, "ordered_ids": ordered_ids}),
    )
    .await?
    .conversations)
}

#[tauri::command]
pub async fn move_chat(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    conversation_id: String,
    chat_group_id: Option<String>,
    target_index: usize,
) -> Result<Conversation, String> {
    Ok(core::<ConversationResult>(
        &app,
        &manager,
        "conversation.move_chat",
        json!({"conversation_id": conversation_id, "chat_group_id": chat_group_id,
               "target_index": target_index}),
    )
    .await?
    .conversation)
}

#[tauri::command]
pub async fn move_work(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    tools: State<'_, Mutex<WorkToolManager>>,
    execution: State<'_, Mutex<AgentExecutionManager>>,
    conversation_id: String,
    project_id: String,
    target_index: usize,
) -> Result<Conversation, String> {
    let current = core::<ConversationResult>(
        &app,
        &manager,
        "conversation.get",
        json!({"conversation_id": conversation_id}),
    )
    .await?
    .conversation;
    if current.kind != "project_work" {
        return Err("Only project work can move between projects".to_string());
    }
    let _search_scope_change = (current.project_id.as_deref() != Some(&project_id)).then(|| {
        app.state::<crate::file_search::FileSearchService>()
            .changing_conversation(&conversation_id)
    });
    if current.project_id.as_deref() != Some(&project_id) {
        let operation_ids = execution
            .lock()
            .map_err(|_| "File executor is unavailable".to_string())?
            .cancel_for_conversation(&conversation_id);
        for operation_id in operation_ids {
            let _: Value = core(
                &app,
                &manager,
                "model.run.tool.resolve",
                json!({"operation_id": operation_id, "success": false,
                       "output": "The work moved to another project.",
                       "error_code": "cancelled_project_move"}),
            )
            .await?;
        }
        tools
            .lock()
            .map_err(|_| "Work permissions are unavailable".to_string())?
            .reset_work_permission(&conversation_id)?;
    }
    Ok(core::<ConversationResult>(
        &app,
        &manager,
        "conversation.move_work",
        json!({"conversation_id": conversation_id, "project_id": project_id,
               "target_index": target_index}),
    )
    .await?
    .conversation)
}

#[tauri::command]
pub async fn copy_work(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    source_conversation_id: String,
    project_id: String,
    title: Option<String>,
) -> Result<Conversation, String> {
    Ok(core::<ConversationResult>(
        &app,
        &manager,
        "conversation.copy_work",
        json!({"source_conversation_id": source_conversation_id,
               "project_id": project_id, "title": title}),
    )
    .await?
    .conversation)
}

#[tauri::command]
pub async fn delete_conversation(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    execution: State<'_, Mutex<AgentExecutionManager>>,
    conversation_id: String,
) -> Result<TrashItem, String> {
    let current = core::<ConversationResult>(
        &app,
        &manager,
        "conversation.get",
        json!({"conversation_id": conversation_id}),
    )
    .await?
    .conversation;
    let _search_scope_change = (current.kind == "project_work").then(|| {
        app.state::<crate::file_search::FileSearchService>()
            .changing_conversation(&conversation_id)
    });
    if current.kind == "project_work" {
        let operation_ids = execution
            .lock()
            .map_err(|_| "File executor is unavailable".to_string())?
            .cancel_for_conversation(&conversation_id);
        for operation_id in operation_ids {
            let _: Value = core(
                &app,
                &manager,
                "model.run.tool.resolve",
                json!({"operation_id": operation_id, "success": false,
                       "output": "The work was moved to Trash.",
                       "error_code": "cancelled_project_deleted"}),
            )
            .await?;
        }
    }
    Ok(core::<TrashItemResult>(
        &app,
        &manager,
        "conversation.delete",
        json!({"conversation_id": conversation_id}),
    )
    .await?
    .trash_item)
}

#[tauri::command]
pub async fn delete_project(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    tools: State<'_, Mutex<WorkToolManager>>,
    execution: State<'_, Mutex<AgentExecutionManager>>,
    project_id: String,
) -> Result<TrashItem, String> {
    let operation_ids = execution
        .lock()
        .map_err(|_| "File executor is unavailable".to_string())?
        .cancel_for_project(&project_id);
    for operation_id in operation_ids {
        let _: Value = core(
            &app,
            &manager,
            "model.run.tool.resolve",
            json!({"operation_id": operation_id, "success": false,
                   "output": "The project was moved to Trash.",
                   "error_code": "cancelled_project_deleted"}),
        )
        .await?;
    }
    {
        let mut tools = tools
            .lock()
            .map_err(|_| "Work tools are unavailable".to_string())?;
        tools.suspend_project(&project_id)?;
        app.state::<crate::file_search::FileSearchService>()
            .invalidate_project(&project_id);
    }
    match core::<TrashItemResult>(
        &app,
        &manager,
        "project.delete",
        json!({"project_id": project_id}),
    )
    .await
    {
        Ok(result) => Ok(result.trash_item),
        Err(error) => {
            let _ = tools
                .lock()
                .map(|mut value| value.restore_project(&project_id));
            Err(error)
        }
    }
}

#[tauri::command]
pub async fn project_delete_impact(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    project_id: String,
) -> Result<ProjectDeleteImpact, String> {
    core(
        &app,
        &manager,
        "project.delete_impact",
        json!({"project_id": project_id}),
    )
    .await
}

#[tauri::command]
pub async fn list_trash(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
) -> Result<Vec<TrashItem>, String> {
    Ok(
        core::<TrashListResult>(&app, &manager, "trash.list", json!({}))
            .await?
            .items,
    )
}

#[tauri::command]
pub async fn restore_trash(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    tools: State<'_, Mutex<WorkToolManager>>,
    entity_type: String,
    entity_id: String,
) -> Result<(), String> {
    let result: TrashMutationResult = core(
        &app,
        &manager,
        "trash.restore",
        json!({"entity_type": entity_type, "entity_id": entity_id}),
    )
    .await?;
    if result.entity_type == "project" {
        tools
            .lock()
            .map_err(|_| "Work tools are unavailable".to_string())?
            .restore_project(&result.id)?;
    }
    Ok(())
}

#[tauri::command]
pub async fn permanently_delete_trash(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    tools: State<'_, Mutex<WorkToolManager>>,
    attachments: State<'_, Mutex<AttachmentStore>>,
    entity_type: String,
    entity_id: String,
) -> Result<(), String> {
    let result: TrashMutationResult = core(
        &app,
        &manager,
        "trash.delete_permanently",
        json!({"entity_type": entity_type, "entity_id": entity_id}),
    )
    .await?;
    if result.entity_type == "project" {
        tools
            .lock()
            .map_err(|_| "Work tools are unavailable".to_string())?
            .remove_project(&result.id)?;
    }
    attachments
        .lock()
        .map_err(|_| "Attachment storage is unavailable".to_string())?
        .delete_storage_keys(&result.storage_keys)?;
    Ok(())
}

#[tauri::command]
pub async fn empty_trash(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    tools: State<'_, Mutex<WorkToolManager>>,
    attachments: State<'_, Mutex<AttachmentStore>>,
) -> Result<(), String> {
    let purged: PurgeResult = core(&app, &manager, "trash.empty", json!({})).await?;
    for project_id in purged.project_ids {
        tools
            .lock()
            .map_err(|_| "Work tools are unavailable".to_string())?
            .remove_project(&project_id)?;
    }
    attachments
        .lock()
        .map_err(|_| "Attachment storage is unavailable".to_string())?
        .delete_storage_keys(&purged.storage_keys)?;
    let _ = purged.conversation_ids;
    Ok(())
}

pub async fn purge_expired_trash(app: &AppHandle) -> Result<(), String> {
    let manager = app.state::<Mutex<CoreProcessManager>>();
    let tools = app.state::<Mutex<WorkToolManager>>();
    let attachments = app.state::<Mutex<AttachmentStore>>();
    let purged: PurgeResult = core(app, &manager, "trash.purge_expired", json!({})).await?;
    for project_id in purged.project_ids {
        tools
            .lock()
            .map_err(|_| "Work tools are unavailable".to_string())?
            .remove_project(&project_id)?;
    }
    attachments
        .lock()
        .map_err(|_| "Attachment storage is unavailable".to_string())?
        .delete_storage_keys(&purged.storage_keys)?;
    let _ = purged.conversation_ids;
    Ok(())
}

#[tauri::command]
pub async fn choose_project_directory(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    tools: State<'_, Mutex<WorkToolManager>>,
    execution: State<'_, Mutex<AgentExecutionManager>>,
    project_id: String,
) -> Result<Option<ProjectGrant>, String> {
    let Some(folder) = rfd::AsyncFileDialog::new().pick_folder().await else {
        return Ok(None);
    };
    let operation_ids = execution
        .lock()
        .map_err(|_| "File executor is unavailable".to_string())?
        .cancel_for_project(&project_id);
    for operation_id in operation_ids {
        let _: Value = core(
            &app,
            &manager,
            "model.run.tool.resolve",
            json!({"operation_id": operation_id, "success": false,
                   "output": "The authorized project directory changed.",
                   "error_code": "cancelled_scope_changed"}),
        )
        .await?;
    }
    let grant = {
        let mut tools = tools
            .lock()
            .map_err(|_| "Work tools are unavailable".to_string())?;
        let grant = tools.set_project_directory(project_id.clone(), folder.path().to_path_buf())?;
        app.state::<crate::file_search::FileSearchService>()
            .invalidate_project(&project_id);
        grant
    };
    Ok(Some(grant))
}

#[tauri::command]
pub fn get_project_grant(
    tools: State<'_, Mutex<WorkToolManager>>,
    project_id: String,
) -> Result<Option<ProjectGrant>, String> {
    Ok(tools
        .lock()
        .map_err(|_| "Work tools are unavailable".to_string())?
        .project_grant(&project_id))
}

#[tauri::command]
pub async fn get_work_permission(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    tools: State<'_, Mutex<WorkToolManager>>,
    conversation_id: String,
) -> Result<WorkPermissionView, String> {
    let conversation = core::<ConversationResult>(
        &app,
        &manager,
        "conversation.get",
        json!({"conversation_id": conversation_id}),
    )
    .await?
    .conversation;
    if conversation.kind != "project_work" {
        return Err(
            "approval_mode_not_allowed: Only project work has an approval mode".to_string(),
        );
    }
    let project_id = conversation
        .project_id
        .ok_or_else(|| "project_not_found: Project work has no project".to_string())?;
    tools
        .lock()
        .map_err(|_| "Work permissions are unavailable".to_string())?
        .work_permission(&conversation.id, &project_id)
}

#[tauri::command]
pub async fn set_work_permission(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    tools: State<'_, Mutex<WorkToolManager>>,
    conversation_id: String,
    mode: ApprovalMode,
) -> Result<WorkPermissionView, String> {
    let conversation = core::<ConversationResult>(
        &app,
        &manager,
        "conversation.get",
        json!({"conversation_id": conversation_id}),
    )
    .await?
    .conversation;
    if conversation.kind != "project_work" {
        return Err(
            "approval_mode_not_allowed: Only project work has an approval mode".to_string(),
        );
    }
    let project_id = conversation
        .project_id
        .ok_or_else(|| "project_not_found: Project work has no project".to_string())?;
    tools
        .lock()
        .map_err(|_| "Work permissions are unavailable".to_string())?
        .set_work_permission(conversation.id, project_id, mode)
}

#[tauri::command]
pub fn list_agent_change_sets(
    execution: State<'_, Mutex<AgentExecutionManager>>,
) -> Result<Vec<ChangeSetView>, String> {
    Ok(execution
        .lock()
        .map_err(|_| "File executor is unavailable".to_string())?
        .list_change_sets())
}

#[tauri::command]
pub async fn resolve_agent_change_set(
    app: AppHandle,
    change_set_id: String,
    approve: bool,
    plan_id: Option<String>,
    plan_hash: Option<String>,
    approved_operation_ids: Option<Vec<String>>,
) -> Result<(), String> {
    let approval = match (plan_id, plan_hash, approved_operation_ids) {
        (Some(plan_id), Some(plan_hash), Some(approved_operation_ids)) => {
            Some(crate::change_set_planner::PlanApproval {
                plan_id,
                plan_hash,
                approved_operation_ids,
            })
        }
        _ => None,
    };
    crate::tool_event_handler::resolve_pending(&app, &change_set_id, approve, approval.as_ref())
        .await
}

#[tauri::command]
pub fn undo_agent_change_set(
    tools: State<'_, Mutex<WorkToolManager>>,
    execution: State<'_, Mutex<AgentExecutionManager>>,
    change_set_id: String,
) -> Result<String, String> {
    let (project_id, _) = execution
        .lock()
        .map_err(|_| "File executor is unavailable".to_string())?
        .undo_target(&change_set_id)?;
    let tools = tools
        .lock()
        .map_err(|_| "Work permissions are unavailable".to_string())?;
    let root = tools.authorized_root(&project_id)?;
    let generation = tools
        .project_grant(&project_id)
        .ok_or_else(|| "undo_recovery_unavailable: Project authorization is missing".to_string())?
        .root_generation;
    execution
        .lock()
        .map_err(|_| "File executor is unavailable".to_string())?
        .undo(&change_set_id, &root, generation)
}

#[tauri::command]
pub fn recover_agent_change_set(
    tools: State<'_, Mutex<WorkToolManager>>,
    execution: State<'_, Mutex<AgentExecutionManager>>,
    change_set_id: String,
) -> Result<ChangeSetView, String> {
    let project_id = execution
        .lock()
        .map_err(|_| "File executor is unavailable".to_string())?
        .recovery_target(&change_set_id)?;
    let tools = tools
        .lock()
        .map_err(|_| "Work permissions are unavailable".to_string())?;
    let root = tools.authorized_root(&project_id)?;
    let generation = tools
        .project_grant(&project_id)
        .ok_or_else(|| "recovery_scope_mismatch: Project authorization is missing".to_string())?
        .root_generation;
    execution
        .lock()
        .map_err(|_| "File executor is unavailable".to_string())?
        .recover_change_set(&change_set_id, &root, generation)
}

#[tauri::command]
pub async fn update_work_purpose(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    conversation_id: String,
    purpose_override: Option<String>,
) -> Result<Conversation, String> {
    Ok(core::<ConversationResult>(
        &app,
        &manager,
        "work.purpose.update",
        json!({
            "conversation_id": conversation_id,
            "purpose_override": purpose_override,
        }),
    )
    .await?
    .conversation)
}

#[tauri::command]
pub async fn list_tool_operations(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    run_id: String,
) -> Result<Value, String> {
    core(
        &app,
        &manager,
        "tool.operation.list",
        json!({"run_id": run_id}),
    )
    .await
}

#[tauri::command]
pub async fn list_conversation_tool_operations(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    conversation_id: String,
) -> Result<Value, String> {
    core(
        &app,
        &manager,
        "tool.operation.list_conversation",
        json!({"conversation_id":conversation_id}),
    )
    .await
}

#[cfg(test)]
mod local_model_policy_tests {
    use super::*;
    #[test]
    fn https_loopback_is_local_for_project_scope_and_fixed_conversation_selection() {
        let mut profile = crate::settings::AppSettings::default()
            .models
            .providers
            .remove(0);
        profile.id = "manual-loopback".into();
        profile.kind = "openai_compatible".into();
        profile.resolved_api_base_url = None;
        for endpoint in [
            "https://127.0.0.1:8443/v1",
            "https://localhost:8443/v1",
            "https://[::1]:8443/v1",
            "http://127.0.0.1:8080/v1",
        ] {
            profile.entered_base_url = endpoint.into();
            assert!(is_local_model_profile(&profile), "{endpoint}");
        }
        for endpoint in [
            "https://remote.test/v1",
            "http://127.0.0.1.evil/v1",
            "http://127.1:8080/v1",
        ] {
            profile.entered_base_url = endpoint.into();
            assert!(!is_local_model_profile(&profile), "{endpoint}");
        }
        profile.id = format!("managed:{}", "a".repeat(64));
        profile.entered_base_url = String::new();
        assert!(is_local_model_profile(&profile));
        profile.id = "demo".into();
        profile.kind = "fake".into();
        assert!(is_local_model_profile(&profile));
    }
}
