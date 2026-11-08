use std::sync::Mutex;

use serde_json::{json, Value};
use tauri::{AppHandle, Manager, State};

use crate::{
    attachment_store::{AttachmentManifest, AttachmentStore},
    core_bridge::request_core,
    core_process_manager::CoreProcessManager,
};

#[tauri::command]
pub async fn get_model_run(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    run_id: String,
) -> Result<Value, String> {
    request_core(&app, &manager, "model.run.get", json!({"run_id": run_id})).await
}

#[tauri::command]
pub async fn get_conversation_model_run(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    conversation_id: String,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "model.run.get",
        json!({"conversation_id": conversation_id}),
    )
    .await
}

#[tauri::command]
pub async fn cancel_model_run(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    run_id: String,
) -> Result<Value, String> {
    app.state::<crate::file_search::FileSearchService>()
        .cancel_run(&run_id);
    request_core(
        &app,
        &manager,
        "model.run.cancel",
        json!({"run_id": run_id}),
    )
    .await
}

#[tauri::command]
pub async fn retry_model_run(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    attachment_store: State<'_, Mutex<AttachmentStore>>,
    run_id: String,
    context_strategy: Option<String>,
    web_search_override: Option<String>,
) -> Result<Value, String> {
    if web_search_override
        .as_deref()
        .is_some_and(|mode| mode != "off")
    {
        crate::offline::deny_network()?;
    }
    normalize_legacy_run_images(&app, &manager, &attachment_store, &run_id).await?;
    request_core(
        &app,
        &manager,
        "model.run.retry",
        json!({
            "run_id": run_id,
            "context_strategy": context_strategy.unwrap_or_else(|| "default".to_string()),
            "web_search_override": web_search_override,
        }),
    )
    .await
}

async fn normalize_legacy_run_images(
    app: &AppHandle,
    manager: &State<'_, Mutex<CoreProcessManager>>,
    attachment_store: &State<'_, Mutex<AttachmentStore>>,
    run_id: &str,
) -> Result<(), String> {
    let run_result = request_core(app, manager, "model.run.get", json!({"run_id": run_id})).await?;
    let run = run_result
        .get("run")
        .ok_or_else(|| "invalid_core_response: Model run is missing.".to_string())?;
    let settings = app.state::<crate::settings::SettingsManager>();
    let configured = crate::settings::commands::catalog_settings(app, manager, &settings).await?;
    let provider_id = run["provider_profile_id"]
        .as_str()
        .ok_or("invalid_core_response: Run provider missing")?;
    let model_id = run["model_id"]
        .as_str()
        .ok_or("invalid_core_response: Run model missing")?;
    if !configured.models.providers.iter().any(|profile| {
        profile.id == provider_id
            && profile.enabled
            && profile
                .models
                .iter()
                .any(|model| model.id == model_id && model.enabled && model.status != "unavailable")
    }) {
        return Err(
            "model_unavailable: Load or reselect an available local model before retrying.".into(),
        );
    }
    let conversation_id = run
        .get("conversation_id")
        .and_then(Value::as_str)
        .ok_or_else(|| "invalid_core_response: Model run conversation is missing.".to_string())?;
    let user_message_id = run
        .get("user_message_id")
        .and_then(Value::as_str)
        .ok_or_else(|| "invalid_core_response: Model run message is missing.".to_string())?;
    let conversation_result = request_core(
        app,
        manager,
        "conversation.get",
        json!({"conversation_id": conversation_id}),
    )
    .await?;
    let attachments = conversation_result
        .get("conversation")
        .and_then(|value| value.get("messages"))
        .and_then(Value::as_array)
        .and_then(|messages| {
            messages
                .iter()
                .find(|message| message.get("id").and_then(Value::as_str) == Some(user_message_id))
        })
        .and_then(|message| message.get("attachments"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    for value in attachments {
        let manifest: AttachmentManifest = serde_json::from_value(value)
            .map_err(|_| "invalid_core_response: Attachment metadata is invalid.".to_string())?;
        if manifest.kind != "image" || manifest.model_storage_key.is_some() {
            continue;
        }
        let normalized = attachment_store
            .lock()
            .map_err(|_| "attachment_store_unavailable: Attachment storage is busy.".to_string())?
            .ensure_model_copy(&manifest)?;
        let storage_key = normalized.model_storage_key.clone().ok_or_else(|| {
            "image_normalization_failed: The image has no safe model-ready copy.".to_string()
        })?;
        let update = request_core(
            app,
            manager,
            "attachment.model_copy.update",
            json!({
                "attachment_id": normalized.id,
                "model_storage_key": &storage_key,
                "model_media_type": normalized.model_media_type,
                "model_byte_size": normalized.model_byte_size,
                "model_sha256": normalized.model_sha256,
                "model_width": normalized.model_width,
                "model_height": normalized.model_height,
                "normalization_version": normalized.normalization_version,
            }),
        )
        .await;
        if let Err(error) = update {
            let _ = attachment_store
                .lock()
                .map_err(|_| {
                    "attachment_store_unavailable: Attachment storage is busy.".to_string()
                })?
                .delete_storage_keys(&[storage_key]);
            return Err(error);
        }
    }
    Ok(())
}
