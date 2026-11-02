use crate::{core_bridge::request_core, core_process_manager::CoreProcessManager};

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SkillDraft {
    name: String,
    description: String,
    content: String,
    expected_revision: Option<String>,
}

#[tauri::command]
pub async fn save_skill(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    draft: SkillDraft,
) -> Result<Value, String> {
    let result = request_core(
        &app,
        &manager,
        "skill.save",
        json!({"name": draft.name, "description": draft.description,
        "content": draft.content, "expected_revision": draft.expected_revision}),
    )
    .await?;
    Ok(result["skill"].clone())
}
use serde_json::{json, Value};
use std::sync::Mutex;
use tauri::{AppHandle, State};

#[tauri::command]
pub async fn list_skills(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
) -> Result<Value, String> {
    let result = request_core(&app, &manager, "skill.list", json!({})).await?;
    Ok(result["skills"].clone())
}

#[tauri::command]
pub async fn import_skill_directory(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
) -> Result<Value, String> {
    let Some(folder) = rfd::AsyncFileDialog::new().pick_folder().await else {
        return Ok(Value::Null);
    };
    let result = request_core(
        &app,
        &manager,
        "skill.import",
        json!({"path": folder.path().to_string_lossy()}),
    )
    .await?;
    Ok(result["skill"].clone())
}

#[tauri::command]
pub async fn inspect_skill(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    name: String,
) -> Result<Value, String> {
    let result = request_core(&app, &manager, "skill.inspect", json!({"name": name})).await?;
    Ok(result["skill"].clone())
}

#[tauri::command]
pub async fn set_skill_enabled(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    skill_id: String,
    enabled: bool,
) -> Result<Value, String> {
    let result = request_core(
        &app,
        &manager,
        "skill.set_enabled",
        json!({"skill_id": skill_id, "enabled": enabled}),
    )
    .await?;
    Ok(result["skills"].clone())
}

#[tauri::command]
pub async fn remove_skill(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    skill_id: String,
) -> Result<Value, String> {
    let result = request_core(
        &app,
        &manager,
        "skill.remove",
        json!({"skill_id": skill_id}),
    )
    .await?;
    Ok(result["skills"].clone())
}
