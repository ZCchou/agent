use crate::{core_bridge::request_core_with_timeout, core_process_manager::CoreProcessManager};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    sync::Mutex,
    time::{Duration, Instant},
};
use tauri::{AppHandle, State};

struct PathGrant {
    kind: String,
    path: PathBuf,
    created: Instant,
}
#[derive(Default)]
pub struct LocalModelGrants {
    grants: HashMap<String, PathGrant>,
}
impl LocalModelGrants {
    fn grant(&mut self, kind: &str, path: &Path) -> Result<Value, String> {
        self.grants
            .retain(|_, grant| grant.created.elapsed() < Duration::from_secs(900));
        if self.grants.len() >= 32 {
            return Err("Too many pending selections. Complete an import first.".into());
        }
        let path = canonical(path)?;
        if (kind == "model"
            && (!path.is_file()
                || path.extension().is_none_or(|e| {
                    e.to_str()
                        .is_none_or(|extension| !extension.eq_ignore_ascii_case("gguf"))
                })))
            || (kind == "root" && !path.is_dir())
        {
            return Err("Select a GGUF file or model storage directory.".into());
        }
        let token = uuid::Uuid::new_v4().to_string();
        let result = json!({"grant_id": token, "path": path});
        self.grants.insert(
            token,
            PathGrant {
                kind: kind.into(),
                path,
                created: Instant::now(),
            },
        );
        Ok(result)
    }
    fn authorize(&mut self, action: &str, params: &mut Value) -> Result<(), String> {
        let kind = match action {
            "model.inspect" | "model.import" => "model",
            "storage.root" => "root",
            _ => return Ok(()),
        };
        let token = params["grant_id"]
            .as_str()
            .ok_or("Choose a file using the native picker.")?
            .to_string();
        let grant = self
            .grants
            .remove(&token)
            .ok_or("File selection expired. Choose again.")?;
        let path = canonical(Path::new(
            params["path"].as_str().ok_or("Expected selected path")?,
        ))?;
        if grant.kind != kind
            || grant.created.elapsed() >= Duration::from_secs(900)
            || path != grant.path
        {
            return Err("File selection expired or does not match the requested path.".into());
        }
        params
            .as_object_mut()
            .ok_or("Expected parameters")?
            .remove("grant_id");
        params["path"] = json!(path);
        if action == "model.inspect" {
            self.grants.insert(token, grant);
        }
        Ok(())
    }
}
fn canonical(path: &Path) -> Result<PathBuf, String> {
    if !path.is_absolute() {
        return Err("An absolute native-selected path is required.".into());
    }
    std::fs::canonicalize(path)
        .map_err(|_| "Selected file or directory is no longer available.".into())
}
fn validate_params(action: &str, params: &Value) -> Result<(), String> {
    let (fields, required): (&[&str], &[&str]) = match action {
        "model.list" | "runtime.unload" | "runtime.status" | "job.list" => (&[], &[]),
        "runtime.capabilities" => (&["verify"], &[]),
        "model.inspect" | "model.import" | "storage.root" => {
            (&["path", "grant_id"], &["path", "grant_id"])
        }
        "model.configure" => (
            &[
                "model_id",
                "device",
                "context_length",
                "gpu_layers",
                "threads",
                "is_default",
                "source",
                "license",
            ],
            &["model_id"],
        ),
        "model.remove" | "runtime.load" => (&["model_id"], &["model_id"]),
        "job.cancel" => (&["job_id"], &["job_id"]),
        _ => return Err("Unsupported local model operation.".into()),
    };
    let object = params.as_object().ok_or("Expected operation parameters")?;
    if required.iter().any(|key| !object.contains_key(*key)) {
        return Err("Missing required operation parameter.".into());
    }
    for (key, value) in object {
        if !fields.contains(&key.as_str()) {
            return Err(format!("Unexpected operation parameter: {key}"));
        }
        let valid = match key.as_str() {
            "model_id" => value.as_str().is_some_and(|s| {
                s.len() == 64
                    && s.bytes()
                        .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
            }),
            "device" => value
                .as_str()
                .is_some_and(|s| matches!(s, "cpu" | "cuda" | "vulkan" | "auto")),
            "context_length" => value
                .as_u64()
                .is_some_and(|n| (1..=16_777_216).contains(&n)),
            "threads" => value.as_u64().is_some_and(|n| (1..=1024).contains(&n)),
            "gpu_layers" => value.as_i64().is_some_and(|n| (-1..=10_000).contains(&n)),
            "is_default" | "verify" => value.is_boolean(),
            _ => value.as_str().is_some_and(|s| {
                (key == "source" || key == "license" || !s.is_empty()) && s.len() <= 32768
            }),
        };
        if !valid {
            return Err(format!("Invalid operation parameter: {key}"));
        }
    }
    Ok(())
}
#[tauri::command]
pub async fn local_model_select_path(
    grants: State<'_, Mutex<LocalModelGrants>>,
    kind: String,
) -> Result<Value, String> {
    let dialog = rfd::AsyncFileDialog::new();
    let selected = match kind.as_str() {
        "model" => dialog.add_filter("GGUF model", &["gguf"]).pick_file().await,
        "root" => dialog.pick_folder().await,
        _ => return Err("Unsupported local model selection.".into()),
    };
    let Some(selected) = selected else {
        return Ok(Value::Null);
    };
    grants
        .lock()
        .map_err(|_| "Unable to authorize file selection")?
        .grant(&kind, selected.path())
}
#[tauri::command]
pub fn local_model_revoke_path(
    grants: State<'_, Mutex<LocalModelGrants>>,
    grant_id: String,
) -> Result<(), String> {
    grants
        .lock()
        .map_err(|_| "Unable to revoke selection")?
        .grants
        .remove(&grant_id);
    Ok(())
}
#[tauri::command]
pub async fn local_model_request(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    grants: State<'_, Mutex<LocalModelGrants>>,
    settings: State<'_, crate::settings::SettingsManager>,
    action: String,
    mut params: Value,
) -> Result<Value, String> {
    validate_params(&action, &params)?;
    grants
        .lock()
        .map_err(|_| "Unable to authorize file selection")?
        .authorize(&action, &mut params)?;
    let default_model = if action == "model.configure" && params["is_default"] == true {
        params["model_id"].as_str().map(str::to_string)
    } else {
        None
    };
    let result = request_core_with_timeout(
        &app,
        &manager,
        "local_model.request",
        json!({"action": action, "params": params}),
        Duration::from_secs(180),
    )
    .await?;
    if let Some(model_id) = default_model {
        settings
            .update_managed_default(&format!("managed:{model_id}"), &model_id)
            .map_err(|error| error.to_string())?;
        let configured =
            crate::settings::commands::catalog_settings(&app, &manager, &settings).await?;
        crate::core_bridge::request_core(
            &app,
            &manager,
            "routing.configure",
            crate::settings::commands::routing_configuration(&configured),
        )
        .await?;
    }
    Ok(result)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_transport_overrides_and_spoofed_ids() {
        for key in [
            "executable",
            "endpoint",
            "api_key",
            "runtime_root",
            "path",
            "token",
        ] {
            assert!(validate_params(
                "runtime.load",
                &json!({"model_id": "a".repeat(64), key: "injected"})
            )
            .is_err());
        }
        assert!(validate_params("runtime.load", &json!({"model_id": "managed:spoof"})).is_err());
        assert!(validate_params("runtime.status", &json!({"path": "C:/x"})).is_err());
        assert!(validate_params(
            "model.configure",
            &json!({"model_id": "a".repeat(64), "gpu_layers": -1, "device": "cpu"})
        )
        .is_ok());
        assert!(validate_params(
            "model.configure",
            &json!({"model_id": "a".repeat(64), "threads": 0})
        )
        .is_err());
    }
    #[test]
    fn native_grants_are_bounded_expiring_kind_and_path_specific() {
        let folder = std::env::temp_dir().join(format!("local-grants-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&folder).unwrap();
        let file = folder.join("main.gguf");
        std::fs::write(&file, b"GGUF").unwrap();
        let other = folder.join("other.gguf");
        std::fs::write(&other, b"GGUF").unwrap();
        let mut grants = LocalModelGrants::default();
        let selection = grants.grant("model", &file).unwrap();
        let mut inspect = selection.clone();
        grants.authorize("model.inspect", &mut inspect).unwrap();
        let mut imported = selection.clone();
        grants.authorize("model.import", &mut imported).unwrap();
        assert!(grants
            .authorize("model.import", &mut selection.clone())
            .is_err());
        let mut mismatch = grants.grant("model", &file).unwrap();
        mismatch["path"] = json!(other);
        assert!(grants.authorize("model.import", &mut mismatch).is_err());
        let mut wrong_kind = grants.grant("root", &folder).unwrap();
        assert!(grants.authorize("model.import", &mut wrong_kind).is_err());
        let mut expired = grants.grant("model", &file).unwrap();
        grants
            .grants
            .get_mut(expired["grant_id"].as_str().unwrap())
            .unwrap()
            .created = Instant::now() - Duration::from_secs(901);
        assert!(grants.authorize("model.import", &mut expired).is_err());
        for _ in 0..32 {
            grants.grant("model", &file).unwrap();
        }
        assert!(grants.grant("model", &file).is_err());
        std::fs::remove_dir_all(folder).unwrap();
    }
}
