use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, State};

use crate::{core_bridge::request_core, core_process_manager::CoreProcessManager};

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CompactionTargetRequest {
    provider_profile_id: String,
    model_id: String,
}

#[tauri::command]
pub async fn get_run_context(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    run_id: String,
) -> Result<Value, String> {
    request_core(&app, &manager, "context.preview", json!({"run_id": run_id})).await
}

#[tauri::command]
pub async fn get_context_source(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    run_id: String,
    item_id: String,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "context.source.preview",
        json!({"run_id": run_id, "item_id": item_id}),
    )
    .await
}

#[tauri::command]
pub async fn continue_model_run_context(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    run_id: String,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "model.run.context.continue",
        json!({"run_id": run_id}),
    )
    .await
}

#[tauri::command]
pub async fn get_context_compaction_status(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    conversation_id: String,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "context.compaction.status",
        json!({"conversation_id": conversation_id}),
    )
    .await
}

#[tauri::command]
pub async fn resume_context_compaction(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    conversation_id: String,
    target: Option<CompactionTargetRequest>,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "context.compaction.resume",
        json!({"conversation_id": conversation_id, "target": target}),
    )
    .await
}

#[cfg(test)]
mod reliability_contract_tests {
    use super::*;

    #[test]
    fn compaction_target_accepts_identity_and_rejects_capability_authority() {
        let target = json!({"provider_profile_id": "allowed", "model_id": "large"});
        let decoded: CompactionTargetRequest = serde_json::from_value(target.clone()).unwrap();
        assert_eq!(serde_json::to_value(decoded).unwrap(), target);
        assert!(serde_json::from_value::<CompactionTargetRequest>(json!({
            "provider_profile_id": "allowed", "model_id": "large", "context_window": 1000000
        }))
        .is_err());
    }
}

#[tauri::command]
pub async fn pause_context_compaction(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    conversation_id: String,
) -> Result<Value, String> {
    request_core(
        &app,
        &manager,
        "context.compaction.pause",
        json!({"conversation_id": conversation_id}),
    )
    .await
}
