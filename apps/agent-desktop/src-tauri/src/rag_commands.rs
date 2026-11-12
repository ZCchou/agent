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
    paths: Vec<PathBuf>,
    created: Instant,
}
#[derive(Default)]
pub struct RagPathGrants {
    grants: HashMap<String, PathGrant>,
}

impl RagPathGrants {
    fn revoke(&mut self, token: &str) {
        self.grants.remove(token);
    }
    fn grant(&mut self, kind: &str, paths: Vec<PathBuf>) -> Result<Value, String> {
        self.grants
            .retain(|_, grant| grant.created.elapsed() < Duration::from_secs(900));
        if self.grants.len() >= 32 {
            return Err(
                "Too many pending file selections. Complete an import or reopen the application."
                    .into(),
            );
        }
        let paths: Vec<PathBuf> = paths
            .iter()
            .map(|path| canonical_path(path))
            .collect::<Result<_, _>>()?;
        let token = uuid::Uuid::new_v4().to_string();
        let result = json!({"grant_id": token, "paths": paths});
        self.grants.insert(
            token,
            PathGrant {
                kind: kind.into(),
                paths,
                created: Instant::now(),
            },
        );
        Ok(result)
    }
    fn authorize(&mut self, action: &str, params: &mut Value) -> Result<(), String> {
        let kind = match action {
            "model.inspect" | "model.import" | "ocr.model.inspect" | "ocr.model.import" => "model",
            "model.root" => "root",
            "document.import" | "document.inspect" => "documents",
            _ => return Ok(()),
        };
        let token = params
            .get("grant_id")
            .and_then(Value::as_str)
            .ok_or("Choose files using the file picker before importing.")?
            .to_string();
        let grant = self
            .grants
            .remove(&token)
            .ok_or("File selection expired. Choose files again.")?;
        if grant.kind != kind || grant.created.elapsed() >= Duration::from_secs(900) {
            return Err("File selection expired or does not match this operation.".into());
        }
        let paths: Vec<PathBuf> = if action == "document.import" || action == "document.inspect" {
            params["paths"]
                .as_array()
                .ok_or("Expected selected document paths")?
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .ok_or_else(|| "Invalid document path".to_string())
                        .and_then(|value| canonical_path(Path::new(value)))
                })
                .collect::<Result<_, _>>()?
        } else {
            vec![canonical_path(Path::new(
                params["path"].as_str().ok_or("Expected selected path")?,
            ))?]
        };
        if paths.is_empty() || paths != grant.paths {
            return Err("The requested paths do not match the file selection.".into());
        }
        params
            .as_object_mut()
            .ok_or("Expected parameters")?
            .remove("grant_id");
        if action == "document.import" || action == "document.inspect" {
            params["paths"] = json!(paths);
        } else {
            params["path"] = json!(paths[0]);
        }
        // Inspection precedes import in the UI and keeps the same bounded grant.
        if action == "model.inspect"
            || action == "ocr.model.inspect"
            || action == "document.inspect"
        {
            self.grants.insert(token, grant);
        }
        Ok(())
    }
}

#[tauri::command]
pub fn rag_revoke_paths(
    grants: State<'_, Mutex<RagPathGrants>>,
    grant_id: String,
) -> Result<(), String> {
    grants
        .lock()
        .map_err(|_| "Unable to revoke file selection")?
        .revoke(&grant_id);
    Ok(())
}

fn canonical_path(path: &Path) -> Result<PathBuf, String> {
    if !path.is_absolute() {
        return Err("An absolute selected path is required.".into());
    }
    std::fs::canonicalize(path)
        .map_err(|_| "Selected file or folder is no longer available.".into())
}

fn validate_params(action: &str, params: &Value) -> Result<(), String> {
    let fields: &[&str] = match action {
        "model.list" | "job.list" | "ocr.model.list" => &[],
        "ocr.model.inspect" | "ocr.model.import" => &["path", "grant_id"],
        "document.ocr.pages" => &["version_id", "page"],
        "document.ocr.render" => &["version_id", "page"],
        "model.inspect" => &["path", "profile", "grant_id"],
        "model.import" => &["path", "profile", "device", "grant_id"],
        "model.root" => &["path", "grant_id"],
        "model.device" => &["model_id", "device"],
        "model.remove" | "model.probe" => &["model_id"],
        "library.list" | "library.overview" => &["project_id"],
        "document.list" => &[
            "library_id",
            "offset",
            "limit",
            "query",
            "include_withdrawn",
        ],
        "document.history" => &["document_id"],
        "document.inspect" => &["library_id", "paths", "grant_id"],
        "library.search" => &["library_id", "query"],
        "job.detail" => &["job_id"],
        "job.resume" => &["job_id", "accept_partial"],
        "library.create" => &["name", "embedding_model_id", "reranker_model_id"],
        "library.update" => &[
            "library_id",
            "name",
            "embedding_model_id",
            "reranker_model_id",
            "ocr_mode",
        ],
        "library.remove" => &["library_id"],
        "library.bind" => &["library_id", "project_id", "enabled"],
        "document.import" => &[
            "library_id",
            "paths",
            "grant_id",
            "ocr_mode",
            "device",
            "auto_index",
            "auto_apply_project_id",
            "profile_id",
            "pages",
            "request_key",
            "decisions",
            "accept_partial",
        ],
        "document.paste" => &["library_id", "title", "content"],
        "document.preview" => &["version_id", "offset", "limit", "page"],
        "document.withdraw" => &["version_id"],
        "document.confirm" | "document.metadata" => &[
            "version_id",
            "revision",
            "source",
            "document_number",
            "title",
            "warnings_acknowledged",
        ],
        "job.cancel" | "job.pause" => &["job_id"],
        "index.build" => &["library_id", "version_ids"],
        "search" => &["project_id", "query", "library_ids", "version_ids", "limit"],
        "evidence.preview" => &["project_id", "evidence_id", "offset", "length"],
        _ => return Err("Unsupported knowledge library operation.".into()),
    };
    let object = params.as_object().ok_or("Expected operation parameters")?;
    for (key, value) in object {
        if !fields.contains(&key.as_str()) {
            return Err(format!("Unexpected operation parameter: {key}"));
        }
        let valid = match key.as_str() {
            "enabled"
            | "warnings_acknowledged"
            | "auto_index"
            | "include_withdrawn"
            | "accept_partial" => value.is_boolean(),
            "offset" | "length" | "limit" | "page" => value.is_u64(),
            "pages" => value.as_array().is_some_and(|items| {
                items.len() <= 10_000
                    && items.iter().all(|item| {
                        item.as_u64()
                            .is_some_and(|page| (1..=10_000).contains(&page))
                    })
            }),
            "version_ids" => value
                .as_array()
                .is_some_and(|items| items.len() <= 100_000 && items.iter().all(Value::is_string)),
            "paths" | "library_ids" => value
                .as_array()
                .is_some_and(|items| items.len() <= 128 && items.iter().all(Value::is_string)),
            "decisions" => value.as_object().is_some_and(|items| {
                items.len() <= 32
                    && items.values().all(|v| {
                        v.as_str()
                            .is_some_and(|s| ["reprocess", "replace", "separate"].contains(&s))
                    })
            }),
            "reranker_model_id" => value.is_null() || value.is_string(),
            _ => value.is_string(),
        };
        if !valid {
            return Err(format!("Invalid operation parameter: {key}"));
        }
    }
    Ok(())
}

#[tauri::command]
pub async fn rag_select_paths(
    grants: State<'_, Mutex<RagPathGrants>>,
    kind: String,
    model_file: Option<bool>,
) -> Result<Value, String> {
    let dialog = rfd::AsyncFileDialog::new();
    let paths = match kind.as_str() {
        "model" if model_file.unwrap_or(false) => dialog
            .add_filter("Model package", &["onnx", "json", "safetensors"])
            .pick_file()
            .await
            .map(|file| vec![file.path().parent().unwrap_or(file.path()).to_path_buf()]),
        "model" | "root" => dialog
            .pick_folder()
            .await
            .map(|folder| vec![folder.path().to_path_buf()]),
        "documents" => dialog
            .add_filter(
                "Documents",
                &[
                    "pdf", "docx", "doc", "docm", "dot", "dotx", "dotm", "rtf", "odt", "wps",
                    "wpt", "xlsx", "xls", "xlsm", "xlt", "xltx", "xltm", "xlsb", "ods", "et",
                    "ett", "csv", "tsv", "ppt", "pptx", "pptm", "pot", "potx", "potm", "pps",
                    "ppsx", "ppsm", "odp", "dps", "dpt", "txt", "md", "markdown",
                ],
            )
            .pick_files()
            .await
            .map(|files| files.iter().map(|file| file.path().to_path_buf()).collect()),
        _ => return Err("Unsupported file selection.".into()),
    };
    let Some(paths) = paths else {
        return Ok(Value::Null);
    };
    grants
        .lock()
        .map_err(|_| "Unable to authorize selected files")?
        .grant(&kind, paths)
}

#[tauri::command]
pub async fn rag_request(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    grants: State<'_, Mutex<RagPathGrants>>,
    action: String,
    mut params: Value,
) -> Result<Value, String> {
    validate_params(&action, &params)?;
    grants
        .lock()
        .map_err(|_| "Unable to authorize selected files")?
        .authorize(&action, &mut params)?;
    request_core_with_timeout(
        &app,
        &manager,
        "rag.request",
        json!({"action": action, "params": params}),
        Duration::from_secs(600),
    )
    .await
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn denies_unknown_actions_and_nested_path_parameters() {
        assert!(validate_params("read_file", &json!({})).is_err());
        assert!(validate_params("search", &json!({"path": "C:/secret"})).is_err());
        assert!(validate_params("model.probe", &json!({"model_id": {"path": "secret"}})).is_err());
        assert!(RagPathGrants::default()
            .authorize("model.import", &mut json!({"path": "C:/secret"}))
            .is_err());
        assert!(validate_params(
            "index.build",
            &json!({"library_id": "library", "version_ids": ["v1", "v2"]})
        )
        .is_ok());
        assert!(validate_params(
            "index.build",
            &json!({"library_id": "library", "version_ids": [{"path": "secret"}]})
        )
        .is_err());
    }
    #[test]
    fn file_grants_are_exact_bounded_and_single_use() {
        let path = std::env::current_exe().unwrap();
        let mut grants = RagPathGrants::default();
        let selection = grants.grant("model", vec![path.clone()]).unwrap();
        let mut params = json!({"path": path, "grant_id": selection["grant_id"]});
        grants.authorize("model.inspect", &mut params).unwrap();
        params["grant_id"] = selection["grant_id"].clone();
        grants.authorize("model.import", &mut params).unwrap();
        params["grant_id"] = selection["grant_id"].clone();
        assert!(grants.authorize("model.import", &mut params).is_err());
        let selection = grants.grant("documents", vec![path]).unwrap();
        assert!(grants
            .authorize(
                "model.root",
                &mut json!({"path": "relative", "grant_id": selection["grant_id"]})
            )
            .is_err());
        assert!(grants.grants.is_empty());
        for _ in 0..32 {
            grants
                .grant("root", vec![std::env::current_dir().unwrap()])
                .unwrap();
        }
        assert!(grants
            .grant("root", vec![std::env::current_dir().unwrap()])
            .is_err());
    }
    #[test]
    fn rejects_expired_changed_and_revoked_selections() {
        let path = std::env::current_dir().unwrap();
        let mut grants = RagPathGrants::default();
        let selection = grants.grant("model", vec![path.clone()]).unwrap();
        let token = selection["grant_id"].as_str().unwrap();
        assert!(grants
            .authorize(
                "model.import",
                &mut json!({"path": std::env::current_exe().unwrap(), "grant_id": token})
            )
            .is_err());
        assert!(!grants.grants.contains_key(token));
        let selection = grants.grant("model", vec![path.clone()]).unwrap();
        let token = selection["grant_id"].as_str().unwrap();
        grants.grants.get_mut(token).unwrap().created = Instant::now() - Duration::from_secs(901);
        assert!(grants
            .authorize(
                "model.import",
                &mut json!({"path": path, "grant_id": token})
            )
            .is_err());
        let selection = grants.grant("root", vec![path.clone()]).unwrap();
        let token = selection["grant_id"].as_str().unwrap();
        grants.revoke(token);
        assert!(grants
            .authorize("model.root", &mut json!({"path": path, "grant_id": token}))
            .is_err());
    }
}

#[tauri::command]
pub async fn rag_open_original(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
    version_id: String,
) -> Result<(), String> {
    use tauri::Manager;
    use tauri_plugin_opener::OpenerExt;
    let result = request_core_with_timeout(
        &app,
        &manager,
        "rag.request",
        json!({"action":"document.original","params":{"version_id":version_id}}),
        Duration::from_secs(30),
    )
    .await?;
    let path = canonical_path(Path::new(
        result["path"].as_str().ok_or("Missing original path")?,
    ))?;
    let root = canonical_path(
        &app.path()
            .app_data_dir()
            .map_err(|e| e.to_string())?
            .join("rag")
            .join("originals"),
    )?;
    if !path.starts_with(root) || !path.is_file() {
        return Err("Original is outside managed storage".into());
    }
    app.opener()
        .open_path(path.to_string_lossy().as_ref(), None::<&str>)
        .map_err(|e| e.to_string())
}
