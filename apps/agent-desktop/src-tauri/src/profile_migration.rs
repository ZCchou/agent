//! Native profile selection and isolated migration-worker management.
use crate::{core_bridge::request_core, core_process_manager::CoreProcessManager};
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
};
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_shell::{process::CommandEvent, ShellExt};

#[cfg(windows)]
use crate::core_transport::CoreTransport as Worker;
#[cfg(not(windows))]
use tauri_plugin_shell::process::CommandChild as Worker;

static BLOCK_CORE: AtomicBool = AtomicBool::new(false);
static FINALIZING: AtomicBool = AtomicBool::new(false);
pub fn blocks_core() -> bool {
    BLOCK_CORE.load(Ordering::SeqCst)
}
pub fn is_finalizing() -> bool {
    FINALIZING.load(Ordering::SeqCst)
}
pub fn is_busy(app: &AppHandle) -> bool {
    blocks_core()
        || app
            .state::<Mutex<MigrationService>>()
            .lock()
            .map(|state| state.running.is_some())
            .unwrap_or(true)
}

#[derive(Default)]
pub struct MigrationService {
    selected_root: Option<PathBuf>,
    running: Option<(String, Worker)>,
}

const LEGACY_PROFILES: [(&str, &str); 2] = [
    ("io.github.zcchou.agent.documentpreview", "Document Preview"),
    ("io.github.zcchou.agent.ocrpreview", "OCR Preview"),
];

#[derive(Serialize)]
struct LegacyProfile {
    id: String,
    label: String,
    root: PathBuf,
    database_bytes: u64,
}

fn discover_profiles(parent: &Path) -> Result<Vec<LegacyProfile>, String> {
    let mut result = Vec::new();
    for (id, label) in LEGACY_PROFILES {
        let root = parent.join(id);
        let database = root.join("agent.db");
        if !database.is_file() {
            continue;
        }
        let root = fs::canonicalize(root).map_err(|_| "migration_source_unavailable")?;
        let bytes = fs::metadata(database)
            .map_err(|_| "migration_source_unavailable")?
            .len();
        result.push(LegacyProfile {
            id: id.into(),
            label: label.into(),
            root,
            database_bytes: bytes,
        });
    }
    Ok(result)
}

fn validate_job_id(value: &str) -> Result<(), String> {
    let parsed = uuid::Uuid::parse_str(value).map_err(|_| "migration_request_invalid")?;
    if parsed.to_string() != value {
        return Err("migration_request_invalid".into());
    }
    Ok(())
}

fn write_json(path: &Path, value: &Value) -> Result<(), String> {
    let temporary = path.with_extension(format!("{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .map_err(|_| "migration_storage_failed")?;
        serde_json::to_writer(&mut file, value).map_err(|_| "migration_storage_failed")?;
        file.flush().map_err(|_| "migration_storage_failed")?;
        file.sync_all().map_err(|_| "migration_storage_failed")?;
        fs::rename(&temporary, path).map_err(|_| "migration_storage_failed".to_string())
    })();
    let _ = fs::remove_file(temporary);
    result
}

fn read_json(path: &Path) -> Result<Value, String> {
    let metadata = fs::symlink_metadata(path).map_err(|_| "migration_state_unavailable")?;
    if !metadata.is_file() || metadata.len() > 64 * 1024 * 1024 {
        return Err("migration_state_invalid".into());
    }
    serde_json::from_slice(&fs::read(path).map_err(|_| "migration_state_unavailable")?)
        .map_err(|_| "migration_state_invalid".into())
}

fn job_path(app: &AppHandle, id: &str) -> Result<PathBuf, String> {
    validate_job_id(id)?;
    Ok(app
        .path()
        .app_data_dir()
        .map_err(|_| "migration_storage_failed")?
        .join("profile-migrations")
        .join(id))
}

fn copy_local_credentials(app: &AppHandle, workspace: &Path) -> Result<(), String> {
    let result = read_json(&workspace.join("candidate.json"))?;
    let candidate = fs::canonicalize(
        result["candidate_root"]
            .as_str()
            .ok_or("migration_state_invalid")?,
    )
    .map_err(|_| "migration_state_invalid")?;
    if !candidate.starts_with(fs::canonicalize(workspace).map_err(|_| "migration_state_invalid")?) {
        return Err("migration_state_invalid".into());
    }
    let settings = read_json(&candidate.join("settings.json"))?;
    let providers = settings["settings"]["models"]["providers"]
        .as_array()
        .ok_or("migration_state_invalid")?;
    let request = read_json(&workspace.join("request.json"))?;
    let sources = request["sources"]
        .as_array()
        .ok_or("migration_state_invalid")?;
    let store = app.state::<crate::credentials::CredentialStore>();
    for descriptor in result["credentials"]
        .as_array()
        .ok_or("migration_state_invalid")?
    {
        if !sources
            .iter()
            .any(|source| source["root"] == descriptor["source_root"])
        {
            continue;
        }
        let id = descriptor["target_id"]
            .as_str()
            .ok_or("migration_state_invalid")?;
        let Some(value) = providers.iter().find(|provider| provider["id"] == id) else {
            return Err("migration_state_invalid".into());
        };
        let profile: crate::settings::model::ProviderProfileSettings =
            serde_json::from_value(value.clone()).map_err(|_| "migration_state_invalid")?;
        if crate::offline::validate_profile(&profile).is_ok() {
            store.copy_legacy_if_missing(
                descriptor["source_id"]
                    .as_str()
                    .ok_or("migration_state_invalid")?,
                id,
            )?;
        }
    }
    Ok(())
}

fn stage_recovery(app: &AppHandle, workspace: &Path) -> Result<(), String> {
    use sha2::{Digest, Sha256};
    let result = read_json(&workspace.join("candidate.json"))?;
    let candidate = fs::canonicalize(
        result["candidate_root"]
            .as_str()
            .ok_or("migration_state_invalid")?,
    )
    .map_err(|_| "migration_state_invalid")?;
    if !candidate.starts_with(fs::canonicalize(workspace).map_err(|_| "migration_state_invalid")?) {
        return Err("migration_state_invalid".into());
    }
    let target = fs::canonicalize(
        app.path()
            .app_data_dir()
            .map_err(|_| "migration_storage_failed")?,
    )
    .map_err(|_| "migration_storage_failed")?;
    let store = app.state::<crate::credentials::CredentialStore>();
    let id = "__installation_recovery_key_v1";
    let secret = store.get(id)?.ok_or("migration_recovery_key_unavailable")?;
    let target_key: [u8; 32] = Sha256::digest(secret.as_bytes()).into();
    for profile in result["profiles"]
        .as_array()
        .ok_or("migration_state_invalid")?
    {
        let root = fs::canonicalize(profile["root"].as_str().ok_or("migration_state_invalid")?)
            .map_err(|_| "migration_state_invalid")?;
        if root != target
            && !LEGACY_PROFILES
                .iter()
                .any(|(id, _)| root.file_name().is_some_and(|name| name == *id))
        {
            return Err("migration_state_invalid".into());
        }
        let source = root.join("file-recovery");
        if !source.is_dir() {
            continue;
        }
        let key = if root == target {
            target_key
        } else {
            let secret = store
                .get_legacy(id)?
                .ok_or("migration_recovery_key_unavailable")?;
            Sha256::digest(secret.as_bytes()).into()
        };
        crate::profile_recovery::stage_source(
            &source,
            &candidate.join("file-recovery"),
            &key,
            &target_key,
            &profile["id_map"],
        )?;
    }
    Ok(())
}

fn prepare_recovery_rollback(app: &AppHandle, workspace: &Path) -> Result<(), String> {
    use sha2::{Digest, Sha256};
    let activation = read_json(&workspace.join("activation.json"))?;
    let Some(entry) = activation["files"]
        .as_array()
        .ok_or("migration_state_invalid")?
        .iter()
        .find(|entry| entry["key"] == "file-recovery/undo-catalog-v1.bin")
    else {
        return Ok(());
    };
    let target = app
        .path()
        .app_data_dir()
        .map_err(|_| "migration_storage_failed")?
        .join("file-recovery/undo-catalog-v1.bin");
    if !target.exists() {
        return Ok(());
    }
    let actual = crate::program_update::file_identity(&target)?;
    if entry["old_hash"] == actual.sha256 || entry["new_hash"] == actual.sha256 {
        return Ok(());
    }
    let result = read_json(&workspace.join("candidate.json"))?;
    let candidate = fs::canonicalize(
        result["candidate_root"]
            .as_str()
            .ok_or("migration_state_invalid")?,
    )
    .map_err(|_| "migration_state_invalid")?;
    if !candidate.starts_with(fs::canonicalize(workspace).map_err(|_| "migration_state_invalid")?) {
        return Err("migration_state_invalid".into());
    }
    let expected = crate::program_update::FileIdentity {
        bytes: entry["new_size"]
            .as_u64()
            .ok_or("migration_state_invalid")?,
        sha256: entry["new_hash"]
            .as_str()
            .ok_or("migration_state_invalid")?
            .into(),
    };
    let secret = app
        .state::<crate::credentials::CredentialStore>()
        .get("__installation_recovery_key_v1")?
        .ok_or("migration_recovery_key_unavailable")?;
    let key: [u8; 32] = Sha256::digest(secret.as_bytes()).into();
    crate::profile_recovery::restore_equivalent_catalog(
        &candidate.join("file-recovery/undo-catalog-v1.bin"),
        &target,
        &key,
        &expected,
    )
}

fn start_worker(app: &AppHandle, id: &str, action: &str) -> Result<(), String> {
    let workspace = job_path(app, id)?;
    let request_path = workspace.join("request.json");
    let mut request = read_json(&request_path)?;
    request["action"] = json!(action);
    write_json(&request_path, &request)?;
    let service = app.state::<Mutex<MigrationService>>();
    let mut state = service.lock().map_err(|_| "migration_busy")?;
    if state.running.is_some() {
        return Err("migration_busy".into());
    }
    let command = app
        .shell()
        .sidecar("agent-core")
        .map_err(|_| "migration_worker_unavailable")?
        .args([
            "--profile-migration",
            request_path.to_str().ok_or("migration_path_invalid")?,
        ]);
    #[cfg(windows)]
    let (mut events, child) = Worker::spawn(command.into())?;
    #[cfg(not(windows))]
    let (mut events, child) = command
        .spawn()
        .map_err(|_| "migration_worker_unavailable")?;
    state.running = Some((id.to_owned(), child));
    drop(state);
    let application = app.clone();
    let id = id.to_owned();
    let action = action.to_owned();
    tauri::async_runtime::spawn(async move {
        let mut output = Vec::new();
        let mut successful = false;
        while let Some(event) = events.recv().await {
            match event {
                CommandEvent::Stdout(bytes) if output.len() + bytes.len() <= 2 * 1024 * 1024 => {
                    output.extend(bytes)
                }
                CommandEvent::Terminated(payload) => {
                    successful = payload.code == Some(0);
                    break;
                }
                _ => {}
            }
        }
        if !successful {
            let last = output
                .split(|byte| *byte == b'\n')
                .filter_map(|line| serde_json::from_slice::<Value>(line).ok())
                .last();
            let mut failure = last.filter(|value| matches!(value["status"].as_str(), Some("failed" | "cancelled")))
                .unwrap_or_else(|| json!({"status":"interrupted","phase":action,"error":{"code":"migration_worker_interrupted"}}));
            failure["phase"] = json!(action);
            let _ = write_json(&workspace.join("state.json"), &failure);
            let pending = read_json(&workspace.join("activation.json"))
                .ok()
                .is_some_and(|record| {
                    !matches!(record["status"].as_str(), Some("completed" | "rolled_back"))
                });
            BLOCK_CORE.store(pending, Ordering::SeqCst);
        }
        if action == "finalize" {
            let manager = application.state::<Mutex<CoreProcessManager>>();
            if request_core(
                &application,
                &manager,
                "app.update.abort",
                json!({"update_id":id}),
            )
            .await
            .is_err()
            {
                if let Ok(mut core) = manager.lock() {
                    let _ = core.shutdown();
                    let _ = core.start(&application);
                }
                let _ = crate::settings::commands::reinject_providers(&application).await;
            }
            FINALIZING.store(false, Ordering::SeqCst);
        }
        if let Ok(mut state) = application.state::<Mutex<MigrationService>>().lock() {
            if state
                .running
                .as_ref()
                .is_some_and(|(running, _)| running == &id)
            {
                state.running.take();
            }
        }
        let _ = application.emit("profile-migration-changed", json!({"job_id":id}));
    });
    Ok(())
}

fn startup_state(mut state: Value, activation: Option<&Value>) -> (Value, bool) {
    if let Some(record) = activation {
        match record["status"].as_str() {
            Some("completed") => {
                if matches!(
                    state["status"].as_str(),
                    Some("awaiting_restart" | "publishing")
                ) {
                    return (json!({"status":"completed","phase":"completed"}), false);
                }
                return (state, false);
            }
            Some("rolled_back") => {
                return (json!({"status":"rolled_back","phase":"rolled_back"}), false)
            }
            _ => {
                return (
                    json!({"status":"interrupted","phase":"publishing","error":{"code":"migration_restart_required"}}),
                    true,
                )
            }
        }
    }
    if matches!(
        state["status"].as_str(),
        Some("running" | "queued" | "publishing")
    ) {
        state["status"] = json!("interrupted");
        state["error"] = json!({"code":"migration_worker_interrupted"});
    }
    (state, false)
}

pub fn initialize(app: &AppHandle) -> Result<(), String> {
    let root = app
        .path()
        .app_data_dir()
        .map_err(|_| "migration_storage_failed")?
        .join("profile-migrations");
    let latest = root.join("latest.json");
    if !latest.exists() {
        return Ok(());
    }
    let value = read_json(&latest)?;
    let id = value["job_id"].as_str().ok_or("migration_state_invalid")?;
    let job = job_path(app, id)?;
    let activation = job.join("activation.json");
    let activation = if activation.exists() {
        Some(read_json(&activation)?)
    } else {
        None
    };
    let prior = read_json(&job.join("state.json"))
        .unwrap_or_else(|_| json!({"status":"queued","phase":"preparing"}));
    let (mut state, blocked) = startup_state(prior, activation.as_ref());
    let transfer_complete = read_json(&job.join("model-transfer.json"))
        .ok()
        .is_some_and(|value| value["status"] == "completed");
    state["finalization_pending"] = json!(
        activation
            .as_ref()
            .is_some_and(|value| value["status"] == "completed")
            && !transfer_complete
            && !job.join("cancel.flag").exists()
    );
    write_json(&job.join("state.json"), &state)?;
    BLOCK_CORE.store(blocked, Ordering::SeqCst);
    Ok(())
}

pub async fn finish_after_startup(app: AppHandle) {
    if crate::program_update_session::is_health_probe() {
        return;
    }
    let Ok(root) = app.path().app_data_dir() else {
        return;
    };
    let Ok(latest) = read_json(&root.join("profile-migrations/latest.json")) else {
        return;
    };
    let Some(id) = latest["job_id"].as_str() else {
        return;
    };
    let Ok(job) = job_path(&app, id) else {
        return;
    };
    if read_json(&job.join("state.json"))
        .ok()
        .is_none_or(|state| state["finalization_pending"] != true)
    {
        return;
    }
    let _ = profile_migration_request(
        app.clone(),
        app.state::<Mutex<MigrationService>>(),
        app.state::<Mutex<CoreProcessManager>>(),
        "finalize".into(),
        Some(id.into()),
        None,
    )
    .await;
}

#[tauri::command]
pub async fn profile_migration_request(
    app: AppHandle,
    service: State<'_, Mutex<MigrationService>>,
    core: State<'_, Mutex<CoreProcessManager>>,
    action: String,
    job_id: Option<String>,
    source_ids: Option<Vec<String>>,
) -> Result<Value, String> {
    let target = app
        .path()
        .app_data_dir()
        .map_err(|_| "migration_storage_failed")?;
    let root = target.join("profile-migrations");
    match action.as_str() {
        "discover" => {
            let profiles = discover_profiles(target.parent().ok_or("migration_path_invalid")?)?;
            let latest = read_json(&root.join("latest.json")).unwrap_or(Value::Null);
            let mut session = service.lock().map_err(|_| "migration_busy")?;
            if session.selected_root.is_none() {
                if let Some(id) = latest["job_id"].as_str() {
                    if let Ok(workspace) = job_path(&app, id) {
                        if let Ok(request) = read_json(&workspace.join("request.json")) {
                            session.selected_root = request["model_root"]
                                .as_str()
                                .and_then(|value| fs::canonicalize(value).ok());
                        }
                    }
                }
            }
            let selected = session.selected_root.clone();
            Ok(
                json!({"profiles":profiles,"model_root":selected,"latest_job_id":latest["job_id"],"recovery_required":blocks_core()}),
            )
        }
        "choose_storage" => {
            let selected = rfd::AsyncFileDialog::new()
                .set_title("选择 agent 独立模型库目录")
                .pick_folder()
                .await;
            let Some(selected) = selected else {
                return Ok(Value::Null);
            };
            let path = fs::canonicalize(selected.path()).map_err(|_| "migration_path_invalid")?;
            service.lock().map_err(|_| "migration_busy")?.selected_root = Some(path.clone());
            Ok(json!({"model_root":path}))
        }
        "prepare" => {
            if blocks_core() {
                return Err("migration_restart_required".into());
            }
            if service
                .lock()
                .map_err(|_| "migration_busy")?
                .running
                .is_some()
            {
                return Err("migration_busy".into());
            }
            let ids = source_ids.ok_or("migration_source_required")?;
            let available = discover_profiles(target.parent().ok_or("migration_path_invalid")?)?;
            if ids.is_empty()
                || ids.len() > 2
                || ids.iter().enumerate().any(|(index, id)| {
                    ids[..index].contains(id) || !available.iter().any(|p| &p.id == id)
                })
            {
                return Err("migration_source_invalid".into());
            }
            let model_root = service
                .lock()
                .map_err(|_| "migration_busy")?
                .selected_root
                .clone()
                .ok_or("migration_model_root_required")?;
            let sources: Vec<Value> = available
                .into_iter()
                .filter(|profile| ids.contains(&profile.id))
                .map(|profile| json!({"root":profile.root,"label":profile.label}))
                .collect();
            let id = uuid::Uuid::new_v4().to_string();
            let workspace = job_path(&app, &id)?;
            fs::create_dir_all(&workspace).map_err(|_| "migration_storage_failed")?;
            write_json(
                &workspace.join("request.json"),
                &json!({"application_id":crate::program_update::APPLICATION_ID,
                "action":"preview","target_root":target,"workspace":workspace,"model_root":model_root,"sources":sources}),
            )?;
            write_json(
                &workspace.join("state.json"),
                &json!({"status":"queued","phase":"preparing"}),
            )?;
            write_json(&root.join("latest.json"), &json!({"job_id":id}))?;
            start_worker(&app, &id, "preview")?;
            Ok(json!({"job_id":id}))
        }
        "confirm_prepare" => {
            if blocks_core()
                || service
                    .lock()
                    .map_err(|_| "migration_busy")?
                    .running
                    .is_some()
            {
                return Err("migration_busy".into());
            }
            let id = job_id.ok_or("migration_job_required")?;
            let workspace = job_path(&app, &id)?;
            let state = read_json(&workspace.join("state.json"))?;
            if state["status"] != "preview_ready" {
                return Err("migration_not_prepared".into());
            }
            if state["preview"]["can_prepare"] != true {
                return Err("migration_insufficient_space".into());
            }
            start_worker(&app, &id, "prepare")?;
            Ok(json!({"job_id":id}))
        }
        "status" => {
            let id = job_id.ok_or("migration_job_required")?;
            let workspace = job_path(&app, &id)?;
            let state = read_json(&workspace.join("state.json"))
                .unwrap_or_else(|_| json!({"status":"queued","phase":"preparing"}));
            let activated = read_json(&workspace.join("activation.json"))
                .ok()
                .is_some_and(|value| value["status"] == "completed");
            Ok(
                json!({"job_id":id,"state":state,"recovery_required":blocks_core(),"activated":activated}),
            )
        }
        "cancel" => {
            let id = job_id.ok_or("migration_job_required")?;
            let workspace = job_path(&app, &id)?;
            let state = read_json(&workspace.join("state.json"))?;
            if matches!(
                state["status"].as_str(),
                Some("publishing" | "awaiting_restart")
            ) {
                return Err("migration_activation_in_progress".into());
            }
            fs::write(workspace.join("cancel.flag"), b"")
                .map_err(|_| "migration_storage_failed")?;
            Ok(json!({"requested":true}))
        }
        "activate" | "rollback" => {
            if service
                .lock()
                .map_err(|_| "migration_busy")?
                .running
                .is_some()
            {
                return Err("migration_busy".into());
            }
            let id = job_id.ok_or("migration_job_required")?;
            if action == "activate" {
                let state = read_json(&job_path(&app, &id)?.join("state.json"))?;
                if state["status"] != "prepared" {
                    return Err("migration_not_prepared".into());
                }
                let candidate = read_json(&job_path(&app, &id)?.join("candidate.json"))?;
                if candidate["credentials"]
                    .as_array()
                    .is_some_and(|items| !items.is_empty())
                {
                    copy_local_credentials(&app, &job_path(&app, &id)?)?;
                }
            }
            if !blocks_core() {
                let prepared =
                    match request_core(&app, &core, "app.update.prepare", json!({"update_id":id}))
                        .await
                    {
                        Ok(value) => value,
                        Err(error) => {
                            let _ = request_core(
                                &app,
                                &core,
                                "app.update.abort",
                                json!({"update_id":id}),
                            )
                            .await;
                            return Err(error);
                        }
                    };
                if prepared["ready"] != true {
                    return Err("migration_application_busy".into());
                }
            }
            BLOCK_CORE.store(true, Ordering::SeqCst);
            let result = (|| {
                app.state::<crate::settings::SettingsManager>()
                    .wait_for_pending_operations()
                    .map_err(|_| "migration_application_busy")?;
                core.lock()
                    .map_err(|_| "migration_application_busy")?
                    .shutdown_for_update()
            })();
            if let Err(error) = result {
                BLOCK_CORE.store(false, Ordering::SeqCst);
                if let Ok(mut manager) = core.lock() {
                    let _ = manager.shutdown();
                    let _ = manager.start(&app);
                }
                return Err(error);
            }
            let started = (|| {
                if action == "activate" {
                    stage_recovery(&app, &job_path(&app, &id)?)?;
                } else {
                    prepare_recovery_rollback(&app, &job_path(&app, &id)?)?;
                }
                start_worker(&app, &id, &action)
            })();
            if let Err(error) = started {
                BLOCK_CORE.store(false, Ordering::SeqCst);
                if let Ok(mut manager) = core.lock() {
                    let _ = manager.start(&app);
                }
                return Err(error);
            }
            Ok(json!({"job_id":id}))
        }
        "finalize" => {
            if blocks_core()
                || service
                    .lock()
                    .map_err(|_| "migration_busy")?
                    .running
                    .is_some()
            {
                return Err("migration_busy".into());
            }
            let id = job_id.ok_or("migration_job_required")?;
            let workspace = job_path(&app, &id)?;
            if read_json(&workspace.join("activation.json"))?["status"] != "completed" {
                return Err("migration_not_prepared".into());
            }
            let _ = request_core(&app, &core, "ping", json!({})).await?;
            let prepared = match request_core(
                &app,
                &core,
                "app.update.prepare",
                json!({"update_id":id}),
            )
            .await
            {
                Ok(value) => value,
                Err(error) => {
                    let _ = request_core(&app, &core, "app.update.abort", json!({"update_id":id}))
                        .await;
                    return Err(error);
                }
            };
            if prepared["ready"] != true {
                return Err("migration_application_busy".into());
            }
            let cancel = workspace.join("cancel.flag");
            if cancel.exists() {
                fs::remove_file(cancel).map_err(|_| "migration_storage_failed")?;
            }
            FINALIZING.store(true, Ordering::SeqCst);
            if let Err(error) = start_worker(&app, &id, "finalize") {
                FINALIZING.store(false, Ordering::SeqCst);
                let _ =
                    request_core(&app, &core, "app.update.abort", json!({"update_id":id})).await;
                return Err(error);
            }
            Ok(json!({"job_id":id}))
        }
        "restart" => {
            app.restart();
        }
        _ => Err("migration_request_invalid".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn discovery_is_limited_to_legacy_agent_profiles() {
        let root = std::env::temp_dir().join(format!("agent-migration-{}", uuid::Uuid::new_v4()));
        for name in [
            "io.github.zcchou.agent.documentpreview",
            "io.github.zcchou.agent.ocrpreview",
            "punkagent",
        ] {
            fs::create_dir_all(root.join(name)).unwrap();
            fs::write(root.join(name).join("agent.db"), b"placeholder").unwrap();
        }
        let profiles = discover_profiles(&root).unwrap();
        assert_eq!(profiles.len(), 2);
        assert!(profiles.iter().all(|profile| profile.id != "punkagent"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn job_identity_cannot_escape_private_migration_directory() {
        assert!(validate_job_id(&uuid::Uuid::new_v4().to_string()).is_ok());
        for value in ["../other", "C:\\other", "job/child", "", "punkagent"] {
            assert!(validate_job_id(value).is_err());
        }
    }

    #[test]
    fn interrupted_activation_exposes_recovery_and_keeps_core_stopped() {
        let (state, blocked) = startup_state(
            json!({"status":"publishing","phase":"publishing"}),
            Some(&json!({"status":"verifying"})),
        );
        assert!(blocked);
        assert_eq!(state["status"], "interrupted");
        let (state, blocked) =
            startup_state(json!({"status":"running","phase":"copying_files"}), None);
        assert!(!blocked);
        assert_eq!(state["status"], "interrupted");
        let (state, blocked) = startup_state(
            json!({"status":"awaiting_restart"}),
            Some(&json!({"status":"completed"})),
        );
        assert!(!blocked);
        assert_eq!(state["status"], "completed");
        let (state, blocked) = startup_state(
            json!({"status":"cleanup_pending","phase":"cleanup_pending"}),
            Some(&json!({"status":"completed"})),
        );
        assert!(!blocked);
        assert_eq!(state["status"], "cleanup_pending");
    }
}
