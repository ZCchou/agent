use std::{
    collections::HashMap,
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
};

use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::{
    attachment_processor::{AttachmentProcessorRegistry, MAX_DOCUMENT_BYTES, MAX_IMAGE_BYTES},
    image_normalizer,
};

const MAX_ATTACHMENTS: usize = 8;
const MAX_TOTAL_BYTES: u64 = 25 * 1024 * 1024;
const MAX_DOCUMENT_TOTAL_BYTES: u64 = 200 * 1024 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AttachmentArtifactManifest {
    pub artifact_id: String,
    pub kind: String,
    pub processor_id: String,
    pub processor_version: String,
    pub media_type: String,
    pub byte_size: u64,
    pub sha256: String,
    pub storage_key: String,
    pub width: Option<u32>,
    pub height: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RejectedAttachmentDescriptor {
    pub receipt_id: String,
    pub display_name: String,
    pub detected_media_type: Option<String>,
    pub error_code: String,
    pub processor_hint: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct AttachmentImportBatchResult {
    pub accepted: Vec<AttachmentManifest>,
    pub rejected: Vec<RejectedAttachmentDescriptor>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AttachmentManifest {
    pub id: String,
    pub kind: String,
    pub name: String,
    pub media_type: String,
    pub byte_size: u64,
    pub sha256: String,
    pub storage_key: String,
    pub width: Option<u32>,
    pub height: Option<u32>,
    #[serde(default)]
    pub model_storage_key: Option<String>,
    #[serde(default)]
    pub model_media_type: Option<String>,
    #[serde(default)]
    pub model_byte_size: Option<u64>,
    #[serde(default)]
    pub model_sha256: Option<String>,
    #[serde(default)]
    pub model_width: Option<u32>,
    #[serde(default)]
    pub model_height: Option<u32>,
    #[serde(default)]
    pub normalization_version: Option<String>,
    #[serde(default)]
    pub artifacts: Vec<AttachmentArtifactManifest>,
}

#[derive(Debug, Clone, Serialize)]
pub struct AttachmentPreview {
    pub id: String,
    pub kind: String,
    pub name: String,
    pub media_type: String,
    pub data_url: Option<String>,
    pub content: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct AttachmentNormalizationStatus {
    pub attachment_id: String,
    pub status: String,
    pub normalization_version: Option<String>,
    pub model_media_type: Option<String>,
    pub model_byte_size: Option<u64>,
    pub model_width: Option<u32>,
    pub model_height: Option<u32>,
    pub error_code: Option<String>,
}

pub struct AttachmentStore {
    app_data_dir: PathBuf,
    root: PathBuf,
    staged: HashMap<String, AttachmentManifest>,
    rejected: HashMap<String, RejectedAttachmentDescriptor>,
    intent_authorizations: HashMap<String, serde_json::Value>,
}

impl AttachmentStore {
    pub fn new(app_data_dir: PathBuf) -> Result<Self, String> {
        let root = app_data_dir.join("message-attachments");
        fs::create_dir_all(&root)
            .map_err(|_| "Could not initialize attachment storage".to_string())?;
        let mut store = Self {
            app_data_dir,
            root: root.clone(),
            staged: HashMap::new(),
            rejected: HashMap::new(),
            intent_authorizations: fs::read(root.join("intent-authorizations.json"))
                .ok()
                .and_then(|bytes| serde_json::from_slice(&bytes).ok())
                .unwrap_or_default(),
        };
        let pending: Vec<_> = store
            .intent_authorizations
            .values()
            .filter(|request| request["__intent_pending"] == true)
            .cloned()
            .collect();
        for request in pending {
            let manifests =
                serde_json::from_value(request["attachments"].clone()).unwrap_or_default();
            let rejections = serde_json::from_value(request["attachment_rejections"].clone())
                .unwrap_or_default();
            store.restore_update_staging(manifests, rejections);
        }
        Ok(store)
    }

    pub fn import_paths(&mut self, paths: &[PathBuf]) -> AttachmentImportBatchResult {
        let mut accepted = Vec::with_capacity(paths.len().min(MAX_ATTACHMENTS));
        let mut rejected = Vec::new();
        let mut total = 0_u64;
        let max_total = batch_total_limit(paths);
        for (index, path) in paths.iter().enumerate() {
            if index >= MAX_ATTACHMENTS {
                let item = self.stage_rejection(
                    path,
                    None,
                    "attachment_count_invalid",
                    Some("select_at_most_eight_files"),
                );
                rejected.push(item);
                continue;
            }
            match self.import_one(path, total, max_total) {
                Ok(manifest) => {
                    total = total.saturating_add(manifest.byte_size);
                    self.staged.insert(manifest.id.clone(), manifest.clone());
                    accepted.push(manifest);
                }
                Err(failure) => {
                    let item = self.stage_rejection(
                        path,
                        failure.detected_media_type.as_deref(),
                        &failure.code,
                        failure.processor_hint.as_deref(),
                    );
                    rejected.push(item);
                }
            }
        }
        if max_total == MAX_DOCUMENT_TOTAL_BYTES
            && !accepted.iter().any(|manifest| {
                manifest
                    .artifacts
                    .iter()
                    .any(|artifact| artifact.kind == "document.source")
            })
        {
            total = 0;
            accepted.retain(|manifest| {
                if total.saturating_add(manifest.byte_size) <= MAX_TOTAL_BYTES {
                    total += manifest.byte_size;
                    return true;
                }
                let _ = self.discard(&manifest.id);
                rejected.push(self.stage_rejection(
                    Path::new(&manifest.name),
                    Some(&manifest.media_type),
                    "attachments_too_large",
                    Some("reduce_attachment_count"),
                ));
                false
            });
        }
        AttachmentImportBatchResult { accepted, rejected }
    }

    pub fn import_clipboard_image(
        &mut self,
        name: &str,
        data_base64: &str,
    ) -> AttachmentImportBatchResult {
        match self.import_clipboard_one(name, data_base64) {
            Ok(manifest) => {
                self.staged.insert(manifest.id.clone(), manifest.clone());
                AttachmentImportBatchResult {
                    accepted: vec![manifest],
                    rejected: vec![],
                }
            }
            Err(failure) => {
                let rejected = self.stage_rejection(
                    Path::new(name),
                    failure.detected_media_type.as_deref(),
                    &failure.code,
                    failure.processor_hint.as_deref(),
                );
                AttachmentImportBatchResult {
                    accepted: vec![],
                    rejected: vec![rejected],
                }
            }
        }
    }

    fn import_clipboard_one(
        &self,
        name: &str,
        data_base64: &str,
    ) -> Result<AttachmentManifest, ImportFailure> {
        if data_base64.len() as u64 > MAX_IMAGE_BYTES.div_ceil(3) * 4 {
            return Err(ImportFailure::new(
                "attachment_too_large",
                None,
                Some("reduce_file_size"),
            ));
        }
        let bytes = STANDARD.decode(data_base64).map_err(|_| {
            ImportFailure::new(
                "attachment_processing_failed",
                None,
                Some("paste_image_again"),
            )
        })?;
        if bytes.len() as u64 > MAX_IMAGE_BYTES {
            return Err(ImportFailure::new(
                "attachment_too_large",
                None,
                Some("reduce_file_size"),
            ));
        }
        let name = safe_file_name(Path::new(name));
        let probe =
            AttachmentProcessorRegistry::probe(Path::new(&name), &bytes).map_err(|error| {
                ImportFailure::new(error.code, error.detected_media_type, error.processor_hint)
            })?;
        let extension = match (probe.kind, probe.media_type) {
            ("image", "image/png") => "png",
            ("image", "image/jpeg") => "jpg",
            ("image", "image/webp") => "webp",
            ("image", "image/gif") => "gif",
            _ => {
                return Err(ImportFailure::new(
                    "attachment_type_unsupported",
                    Some(probe.media_type),
                    None,
                ))
            }
        };
        // A generated private input lets the existing file-based image pipeline
        // validate, normalize and snapshot clipboard bytes identically to files.
        let temporary =
            self.root
                .join(format!(".clipboard-{}.{}", uuid::Uuid::new_v4(), extension));
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .map_err(|_| {
                ImportFailure::new(
                    "attachment_store_failed",
                    Some(probe.media_type),
                    Some("retry"),
                )
            })?;
        let written = file.write_all(&bytes);
        drop(file);
        let result = if written.is_ok() {
            self.import_one(&temporary, 0, MAX_TOTAL_BYTES)
        } else {
            Err(ImportFailure::new(
                "attachment_store_failed",
                Some(probe.media_type),
                Some("retry"),
            ))
        };
        let _ = fs::remove_file(&temporary);
        result.map(|mut manifest| {
            manifest.name = name;
            manifest
        })
    }

    fn import_one(
        &self,
        path: &Path,
        current_total: u64,
        max_total: u64,
    ) -> Result<AttachmentManifest, ImportFailure> {
        let metadata = fs::metadata(path).map_err(|_| {
            ImportFailure::new("attachment_unavailable", None, Some("select_file_again"))
        })?;
        if metadata.is_dir() {
            return Err(ImportFailure::new(
                "attachment_directory_unsupported",
                None,
                Some("select_files_instead"),
            ));
        }
        if !metadata.is_file() {
            return Err(ImportFailure::new(
                "attachment_unavailable",
                None,
                Some("select_file_again"),
            ));
        }
        if metadata.len() > MAX_DOCUMENT_BYTES {
            return Err(ImportFailure::new(
                "attachment_too_large",
                None,
                Some("reduce_file_size"),
            ));
        }
        let bytes = fs::read(path).map_err(|_| {
            ImportFailure::new("attachment_unavailable", None, Some("select_file_again"))
        })?;
        let probe = AttachmentProcessorRegistry::probe(path, &bytes).map_err(|error| {
            ImportFailure::new(error.code, error.detected_media_type, error.processor_hint)
        })?;
        if bytes.len() as u64 > probe.max_bytes {
            return Err(ImportFailure::new(
                "attachment_too_large",
                Some(probe.media_type),
                Some("reduce_file_size"),
            ));
        }
        let max_total = if probe.artifact_kind == "document.source" {
            MAX_DOCUMENT_TOTAL_BYTES
        } else {
            max_total
        };
        if current_total.saturating_add(bytes.len() as u64) > max_total {
            return Err(ImportFailure::new(
                "attachments_too_large",
                Some(probe.media_type),
                Some("reduce_attachment_count"),
            ));
        }
        let id = uuid::Uuid::new_v4().to_string();
        let safe_name = safe_file_name(path);
        let storage_name = format!("{id}.bin");
        let destination = self.root.join(&storage_name);
        let temporary = self.root.join(format!(".{id}.tmp"));
        let normalized = if probe.kind == "image" {
            Some(
                image_normalizer::normalize(path, probe.media_type).map_err(|error| {
                    ImportFailure::from_message(
                        &error,
                        Some(probe.media_type),
                        Some("select_a_valid_image"),
                    )
                })?,
            )
        } else {
            None
        };
        fs::write(&temporary, &bytes).map_err(|_| {
            ImportFailure::new(
                "attachment_store_failed",
                Some(probe.media_type),
                Some("retry"),
            )
        })?;
        if fs::rename(&temporary, &destination).is_err() {
            let _ = fs::remove_file(&temporary);
            return Err(ImportFailure::new(
                "attachment_store_failed",
                Some(probe.media_type),
                Some("retry"),
            ));
        }
        let (
            model_storage_key,
            model_media_type,
            model_byte_size,
            model_sha256,
            model_width,
            model_height,
            normalization_version,
        ) = if let Some(normalized) = normalized {
            let model_name = format!("{id}.model.{}", normalized.extension);
            let model_destination = self.root.join(&model_name);
            let model_temporary = self.root.join(format!(".{id}.model.tmp"));
            if let Err(error) = fs::write(&model_temporary, &normalized.bytes)
                .and_then(|_| fs::rename(&model_temporary, &model_destination))
            {
                let _ = fs::remove_file(&destination);
                let _ = fs::remove_file(&model_temporary);
                return Err(ImportFailure::from_message(
                    &format!("attachment_store_failed: {error}"),
                    Some(probe.media_type),
                    Some("retry"),
                ));
            }
            (
                Some(format!("message-attachments/{model_name}")),
                Some(normalized.media_type.to_string()),
                Some(normalized.bytes.len() as u64),
                Some(normalized.sha256),
                Some(normalized.width),
                Some(normalized.height),
                Some("image-normalization-v1".to_string()),
            )
        } else {
            (None, None, None, None, None, None, None)
        };
        let dimensions = if probe.kind == "image" {
            image::image_dimensions(path).ok()
        } else {
            None
        };
        let manifest = AttachmentManifest {
            id: id.clone(),
            kind: probe.kind.to_string(),
            name: safe_name,
            media_type: probe.media_type.to_string(),
            byte_size: bytes.len() as u64,
            sha256: format!("{:x}", Sha256::digest(&bytes)),
            storage_key: format!("message-attachments/{storage_name}"),
            width: dimensions.map(|value| value.0),
            height: dimensions.map(|value| value.1),
            model_storage_key,
            model_media_type,
            model_byte_size,
            model_sha256,
            model_width,
            model_height,
            normalization_version,
            artifacts: Vec::new(),
        };
        let artifact = if manifest.kind == "image" {
            AttachmentArtifactManifest {
                artifact_id: uuid::Uuid::new_v4().to_string(),
                kind: probe.artifact_kind.to_string(),
                processor_id: probe.processor_id.to_string(),
                processor_version: probe.processor_version.to_string(),
                media_type: manifest.model_media_type.clone().unwrap_or_default(),
                byte_size: manifest.model_byte_size.unwrap_or_default(),
                sha256: manifest.model_sha256.clone().unwrap_or_default(),
                storage_key: manifest.model_storage_key.clone().unwrap_or_default(),
                width: manifest.model_width,
                height: manifest.model_height,
            }
        } else {
            AttachmentArtifactManifest {
                artifact_id: uuid::Uuid::new_v4().to_string(),
                kind: probe.artifact_kind.to_string(),
                processor_id: probe.processor_id.to_string(),
                processor_version: probe.processor_version.to_string(),
                media_type: manifest.media_type.clone(),
                byte_size: manifest.byte_size,
                sha256: manifest.sha256.clone(),
                storage_key: manifest.storage_key.clone(),
                width: None,
                height: None,
            }
        };
        let mut manifest = manifest;
        manifest.artifacts.push(artifact);
        Ok(manifest)
    }

    fn stage_rejection(
        &mut self,
        path: &Path,
        detected_media_type: Option<&str>,
        error_code: &str,
        processor_hint: Option<&str>,
    ) -> RejectedAttachmentDescriptor {
        let item = RejectedAttachmentDescriptor {
            receipt_id: uuid::Uuid::new_v4().to_string(),
            display_name: safe_file_name(path),
            detected_media_type: detected_media_type.map(str::to_string),
            error_code: error_code.to_string(),
            processor_hint: processor_hint.map(str::to_string),
        };
        self.rejected.insert(item.receipt_id.clone(), item.clone());
        item
    }

    pub fn resolve(&self, ids: &[String]) -> Result<Vec<AttachmentManifest>, String> {
        if ids.len() > MAX_ATTACHMENTS {
            return Err(
                "attachment_count_invalid: A message can contain at most 8 attachments."
                    .to_string(),
            );
        }
        ids.iter()
            .map(|id| {
                self.staged.get(id).cloned().ok_or_else(|| {
                    "attachment_staging_expired: An attachment must be selected again.".to_string()
                })
            })
            .collect()
    }

    pub fn restore_update_staging(
        &mut self,
        manifests: Vec<AttachmentManifest>,
        rejected: Vec<RejectedAttachmentDescriptor>,
    ) -> (
        Vec<AttachmentManifest>,
        Vec<RejectedAttachmentDescriptor>,
        usize,
    ) {
        let mut restored = Vec::new();
        let mut missing = 0;
        for manifest in manifests.into_iter().take(MAX_ATTACHMENTS) {
            if self.preview_manifest(&manifest).is_err() {
                missing += 1;
                continue;
            }
            self.staged.insert(manifest.id.clone(), manifest.clone());
            restored.push(manifest);
        }
        let rejected: Vec<_> = rejected.into_iter().take(MAX_ATTACHMENTS).collect();
        for descriptor in &rejected {
            self.rejected
                .insert(descriptor.receipt_id.clone(), descriptor.clone());
        }
        (restored, rejected, missing)
    }

    pub fn resolve_rejections(
        &self,
        ids: &[String],
    ) -> Result<Vec<RejectedAttachmentDescriptor>, String> {
        if ids.len() > MAX_ATTACHMENTS {
            return Err(
                "attachment_count_invalid: A message can contain at most 8 rejected attachments."
                    .to_string(),
            );
        }
        ids.iter()
            .map(|id| {
                self.rejected.get(id).cloned().ok_or_else(|| {
                    "attachment_staging_expired: An attachment rejection must be selected again."
                        .to_string()
                })
            })
            .collect()
    }

    /// Create the model-safe derivative for an attachment saved before schema v12.
    ///
    /// The original snapshot remains immutable. This method is intentionally owned
    /// by the Host so retries of legacy WebP/GIF images pass through the exact same
    /// decoder, metadata stripping and size limits as newly selected images.
    pub fn ensure_model_copy(
        &mut self,
        manifest: &AttachmentManifest,
    ) -> Result<AttachmentManifest, String> {
        if manifest.kind != "image" {
            return Err("image_format_unsupported: The attachment is not an image.".to_string());
        }
        if manifest.model_storage_key.is_some() {
            return Ok(manifest.clone());
        }
        let source_path = self.safe_path(&manifest.storage_key)?;
        let source_bytes = fs::read(&source_path).map_err(|_| {
            "attachment_unavailable: Attachment snapshot is unavailable.".to_string()
        })?;
        if format!("{:x}", Sha256::digest(&source_bytes)) != manifest.sha256 {
            return Err(
                "image_integrity_failed: Attachment snapshot failed integrity validation."
                    .to_string(),
            );
        }
        let normalized = image_normalizer::normalize(&source_path, &manifest.media_type)?;
        let model_name = format!(
            "{}.model-{}.{}",
            manifest.id,
            uuid::Uuid::new_v4(),
            normalized.extension
        );
        let model_destination = self.root.join(&model_name);
        let model_temporary = self.root.join(format!(
            ".{}.model-{}.tmp",
            manifest.id,
            uuid::Uuid::new_v4()
        ));
        fs::write(&model_temporary, &normalized.bytes)
            .and_then(|_| fs::rename(&model_temporary, &model_destination))
            .map_err(|error| {
                let _ = fs::remove_file(&model_temporary);
                format!(
                    "attachment_store_failed: The model image copy could not be stored: {error}"
                )
            })?;
        let mut updated = manifest.clone();
        updated.model_storage_key = Some(format!("message-attachments/{model_name}"));
        updated.model_media_type = Some(normalized.media_type.to_string());
        updated.model_byte_size = Some(normalized.bytes.len() as u64);
        updated.model_sha256 = Some(normalized.sha256);
        updated.model_width = Some(normalized.width);
        updated.model_height = Some(normalized.height);
        updated.normalization_version = Some("image-normalization-v1".to_string());
        if self.staged.contains_key(&manifest.id) {
            self.staged.insert(manifest.id.clone(), updated.clone());
        }
        Ok(updated)
    }

    pub fn authorize_intent(
        &mut self,
        task_id: &str,
        mut request: serde_json::Value,
    ) -> Result<(), String> {
        request["__intent_pending"] = serde_json::json!(true);
        self.intent_authorizations
            .insert(task_id.to_string(), request);
        self.persist_intent_authorizations()
    }

    fn persist_intent_authorizations(&self) -> Result<(), String> {
        let bytes =
            serde_json::to_vec(&self.intent_authorizations).map_err(|error| error.to_string())?;
        let temporary = self.root.join("intent-authorizations.pending");
        fs::write(&temporary, bytes).map_err(|error| error.to_string())?;
        fs::rename(temporary, self.root.join("intent-authorizations.json"))
            .map_err(|error| error.to_string())
    }

    pub fn reconcile_intent(&mut self, task: &serde_json::Value) -> Result<(), String> {
        let id = task
            .get("id")
            .and_then(serde_json::Value::as_str)
            .ok_or("intent_task_invalid")?;
        let request = &task["request"];
        let request_key = format!(
            "request:{}",
            request["idempotency_key"].as_str().unwrap_or("")
        );
        let authorized = self
            .intent_authorizations
            .get(id)
            .or_else(|| self.intent_authorizations.get(&request_key))
            .ok_or("intent_authorization_missing")?
            .clone();
        for field in [
            "idempotency_key",
            "attachments",
            "attachment_rejections",
            "attachment_staging_ids",
            "attachment_rejection_staging_ids",
            "provider_profile_id",
            "model_id",
            "selection",
            "project_id",
            "conversation_id",
        ] {
            if authorized.get(field) != request.get(field) {
                return Err("intent_authorization_mismatch".into());
            }
        }
        self.intent_authorizations.remove(&request_key);
        self.intent_authorizations
            .insert(id.to_string(), authorized.clone());
        let accepted = task["status"] == "completed"
            && matches!(
                task["result"]["status"].as_str(),
                Some("accepted" | "waiting_permission" | "waiting_budget")
            )
            && task["result"]["run"]["id"].is_string()
            && task["result"]["conversation"]["id"].is_string();
        if matches!(task["status"].as_str(), Some("completed" | "cancelled")) {
            if let Some(saved) = self.intent_authorizations.get_mut(id) {
                saved["__intent_pending"] = serde_json::json!(false);
            }
            self.persist_intent_authorizations()?;
        }
        if accepted {
            let ids: Vec<String> =
                serde_json::from_value(authorized["attachment_staging_ids"].clone())
                    .unwrap_or_default();
            let rejected: Vec<String> =
                serde_json::from_value(authorized["attachment_rejection_staging_ids"].clone())
                    .unwrap_or_default();
            self.mark_committed(&ids);
            self.mark_rejections_committed(&rejected);
        }
        Ok(())
    }

    pub fn mark_committed(&mut self, ids: &[String]) {
        for id in ids {
            self.staged.remove(id);
        }
    }

    pub fn mark_rejections_committed(&mut self, ids: &[String]) {
        for id in ids {
            self.rejected.remove(id);
        }
    }

    pub fn discard(&mut self, id: &str) -> Result<(), String> {
        if self.intent_authorizations.values().any(|request| {
            request["__intent_pending"] == true
                && ["attachment_staging_ids", "attachment_rejection_staging_ids"]
                    .iter()
                    .any(|field| {
                        request[*field]
                            .as_array()
                            .is_some_and(|ids| ids.iter().any(|value| value.as_str() == Some(id)))
                    })
        }) {
            return Ok(());
        }
        if self.rejected.remove(id).is_some() {
            return Ok(());
        }
        let manifest = self
            .staged
            .remove(id)
            .ok_or_else(|| "attachment_not_found: Attachment is not staged.".to_string())?;
        let path = self.safe_path(&manifest.storage_key)?;
        if path.exists() {
            fs::remove_file(path).map_err(|_| {
                "attachment_delete_failed: Attachment could not be removed.".to_string()
            })?;
        }
        if let Some(model_storage_key) = manifest.model_storage_key.as_deref() {
            let model_path = self.safe_path(model_storage_key)?;
            if model_path.exists() {
                fs::remove_file(model_path).map_err(|_| {
                    "attachment_delete_failed: Model image copy could not be removed.".to_string()
                })?;
            }
        }
        Ok(())
    }

    pub fn delete_storage_keys(&mut self, storage_keys: &[String]) -> Result<(), String> {
        for storage_key in storage_keys {
            let path = match self.safe_path(storage_key) {
                Ok(path) => path,
                Err(error) if error.starts_with("attachment_unavailable") => continue,
                Err(error) => return Err(error),
            };
            if path.exists() {
                fs::remove_file(path).map_err(|_| {
                    "attachment_delete_failed: Attachment snapshot could not be removed."
                        .to_string()
                })?;
            }
            self.staged.retain(|_, manifest| {
                manifest.storage_key != *storage_key
                    && manifest.model_storage_key.as_deref() != Some(storage_key)
            });
        }
        Ok(())
    }

    pub fn preview(&self, id: &str) -> Result<AttachmentPreview, String> {
        let manifest = self
            .staged
            .get(id)
            .ok_or_else(|| "attachment_not_found: Attachment is not staged.".to_string())?;
        self.preview_manifest(manifest)
    }

    pub fn preview_manifest(
        &self,
        manifest: &AttachmentManifest,
    ) -> Result<AttachmentPreview, String> {
        let bytes = fs::read(self.safe_path(&manifest.storage_key)?).map_err(|_| {
            "attachment_unavailable: Attachment snapshot is unavailable.".to_string()
        })?;
        if bytes.len() as u64 != manifest.byte_size
            || format!("{:x}", Sha256::digest(&bytes)) != manifest.sha256
        {
            return Err(
                "attachment_changed: Attachment snapshot failed integrity validation.".to_string(),
            );
        }
        let (data_url, content) = if manifest.kind == "image" {
            (
                Some(format!(
                    "data:{};base64,{}",
                    manifest.media_type,
                    STANDARD.encode(bytes)
                )),
                None,
            )
        } else if manifest
            .artifacts
            .iter()
            .any(|artifact| artifact.kind == "document.source")
        {
            let content = if let Some(artifact) = manifest
                .artifacts
                .iter()
                .find(|artifact| artifact.kind == "text.utf8")
            {
                let text_bytes =
                    fs::read(self.safe_path(&artifact.storage_key)?).map_err(|_| {
                        "attachment_unavailable: Extracted document text is unavailable."
                            .to_string()
                    })?;
                if text_bytes.len() as u64 != artifact.byte_size
                    || format!("{:x}", Sha256::digest(&text_bytes)) != artifact.sha256
                {
                    return Err(
                        "attachment_changed: Extracted document text failed integrity validation."
                            .to_string(),
                    );
                }
                String::from_utf8(text_bytes).map_err(|_| {
                    "invalid_attachment_encoding: Extracted document text is not UTF-8.".to_string()
                })?
            } else {
                "Document pending extraction.".to_string()
            };
            (None, Some(content))
        } else {
            (
                None,
                Some(String::from_utf8(bytes).map_err(|_| {
                    "invalid_attachment_encoding: Text attachment is not UTF-8.".to_string()
                })?),
            )
        };
        Ok(AttachmentPreview {
            id: manifest.id.clone(),
            kind: manifest.kind.clone(),
            name: manifest.name.clone(),
            media_type: manifest.media_type.clone(),
            data_url,
            content,
        })
    }

    pub fn normalization_status(
        &self,
        manifest: &AttachmentManifest,
    ) -> AttachmentNormalizationStatus {
        let (status, error_code) = if manifest.kind != "image" {
            ("not_applicable", None)
        } else if manifest.model_storage_key.is_none() {
            ("legacy_pending", None)
        } else {
            let validated = manifest
                .model_storage_key
                .as_deref()
                .and_then(|key| self.safe_path(key).ok())
                .and_then(|path| fs::read(path).ok())
                .is_some_and(|bytes| {
                    let digest = format!("{:x}", Sha256::digest(&bytes));
                    manifest.model_byte_size == Some(bytes.len() as u64)
                        && manifest.model_sha256.as_deref() == Some(digest.as_str())
                });
            if validated {
                ("ready", None)
            } else {
                ("failed", Some("image_integrity_failed"))
            }
        };
        AttachmentNormalizationStatus {
            attachment_id: manifest.id.clone(),
            status: status.to_string(),
            normalization_version: manifest.normalization_version.clone(),
            model_media_type: manifest.model_media_type.clone(),
            model_byte_size: manifest.model_byte_size,
            model_width: manifest.model_width,
            model_height: manifest.model_height,
            error_code: error_code.map(str::to_string),
        }
    }

    fn safe_path(&self, storage_key: &str) -> Result<PathBuf, String> {
        let relative = Path::new(storage_key);
        if relative.is_absolute()
            || relative
                .components()
                .any(|component| !matches!(component, std::path::Component::Normal(_)))
            || !storage_key
                .replace('\\', "/")
                .starts_with("message-attachments/")
        {
            return Err("invalid_attachment_path: Attachment path is invalid.".to_string());
        }
        let candidate = self.app_data_dir.join(relative);
        let canonical_root = self.root.canonicalize().map_err(|_| {
            "attachment_store_unavailable: Attachment storage is unavailable.".to_string()
        })?;
        let canonical = candidate.canonicalize().map_err(|_| {
            "attachment_unavailable: Attachment snapshot is unavailable.".to_string()
        })?;
        if !canonical.starts_with(&canonical_root) {
            return Err("invalid_attachment_path: Attachment path is invalid.".to_string());
        }
        Ok(canonical)
    }
}

fn batch_total_limit(paths: &[PathBuf]) -> u64 {
    let has_document = paths.iter().take(MAX_ATTACHMENTS).any(|path| {
        let Ok(mut file) = fs::File::open(path) else {
            return false;
        };
        if !file
            .metadata()
            .is_ok_and(|metadata| metadata.is_file() && metadata.len() <= MAX_DOCUMENT_BYTES)
        {
            return false;
        }
        let mut header = [0_u8; 12];
        let Ok(count) = file.read(&mut header) else {
            return false;
        };
        AttachmentProcessorRegistry::probe(path, &header[..count])
            .is_ok_and(|probe| probe.artifact_kind == "document.source")
    });
    if has_document {
        MAX_DOCUMENT_TOTAL_BYTES
    } else {
        MAX_TOTAL_BYTES
    }
}

#[derive(Debug)]
struct ImportFailure {
    code: String,
    detected_media_type: Option<String>,
    processor_hint: Option<String>,
}

impl ImportFailure {
    fn new(code: &str, detected: Option<&str>, hint: Option<&str>) -> Self {
        Self {
            code: code.to_string(),
            detected_media_type: detected.map(str::to_string),
            processor_hint: hint.map(str::to_string),
        }
    }

    fn from_message(message: &str, detected: Option<&str>, hint: Option<&str>) -> Self {
        let code = message
            .split(':')
            .next()
            .unwrap_or("attachment_processing_failed");
        Self::new(code, detected, hint)
    }
}

fn safe_file_name(path: &Path) -> String {
    let value = path
        .file_name()
        .and_then(|value| value.to_str())
        .filter(|value| !value.is_empty())
        .unwrap_or("unnamed");
    let result: String = value
        .chars()
        .filter(|character| !character.is_control())
        .take(255)
        .collect();
    if result.is_empty() {
        "unnamed".to_string()
    } else {
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pending_intent_pins_staging_and_restores_authorization_after_restart() {
        let root = test_root();
        fs::create_dir_all(&root).unwrap();
        let source = root.join("notes.txt");
        fs::write(&source, "Keep the original attachment.").unwrap();
        let mut store = AttachmentStore::new(root.clone()).unwrap();
        let manifest = store.import_paths(&[source]).accepted.remove(0);
        let request = serde_json::json!({"idempotency_key": "once", "provider_profile_id": "local", "model_id": "text",
            "attachment_staging_ids": [manifest.id], "attachment_rejection_staging_ids": [],
            "attachments": [manifest], "attachment_rejections": []});
        store.authorize_intent("intent1", request.clone()).unwrap();
        store.discard(&manifest.id).unwrap();
        assert!(root.join(&manifest.storage_key).exists());
        drop(store);
        let mut store = AttachmentStore::new(root.clone()).unwrap();
        assert_eq!(
            store.resolve(std::slice::from_ref(&manifest.id)).unwrap()[0].id,
            manifest.id
        );
        let mut task =
            serde_json::json!({"id": "intent1", "status": "waiting_user", "request": request});
        task["request"]["model_id"] = serde_json::json!("forged");
        assert!(store.reconcile_intent(&task).is_err());
        task["request"]["model_id"] = serde_json::json!("text");
        task["status"] = serde_json::json!("cancelled");
        store.reconcile_intent(&task).unwrap();
        store.discard(&manifest.id).unwrap();
        assert!(!root.join(&manifest.storage_key).exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn document_source_preview_retention_and_discard_preserve_binary_snapshot() {
        let root = test_root();
        fs::create_dir_all(&root).unwrap();
        let source = root.join("report.docx");
        let bytes = b"PK\x03\x04\xff\xfe";
        fs::write(&source, bytes).unwrap();
        let mut store = AttachmentStore::new(root.clone()).unwrap();
        let result = store.import_paths(&[source]);
        assert!(result.rejected.is_empty());
        let manifest = result.accepted[0].clone();
        assert_eq!(manifest.artifacts[0].kind, "document.source");
        assert_eq!(fs::read(root.join(&manifest.storage_key)).unwrap(), bytes);
        assert_eq!(
            store.preview(&manifest.id).unwrap().content.as_deref(),
            Some("Document pending extraction.")
        );
        let (restored, _, missing) = store.restore_update_staging(vec![manifest.clone()], vec![]);
        assert_eq!(restored.len(), 1);
        assert_eq!(missing, 0);
        store.discard(&manifest.id).unwrap();
        assert!(!root.join(&manifest.storage_key).exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn document_preview_checks_original_and_extracted_text_integrity() {
        let root = test_root();
        fs::create_dir_all(&root).unwrap();
        let source = root.join("report.docx");
        let original = b"PK\x03\x04\xff";
        fs::write(&source, original).unwrap();
        let mut store = AttachmentStore::new(root.clone()).unwrap();
        let mut manifest = store.import_paths(&[source]).accepted.remove(0);
        let content = "Document paragraph";
        let storage_key = "message-attachments/extracted.txt";
        fs::write(root.join(storage_key), content).unwrap();
        let mut artifact = manifest.artifacts[0].clone();
        artifact.kind = "text.utf8".to_string();
        artifact.media_type = "text/plain".to_string();
        artifact.storage_key = storage_key.to_string();
        artifact.byte_size = content.len() as u64;
        artifact.sha256 = format!("{:x}", Sha256::digest(content.as_bytes()));
        manifest.artifacts.push(artifact);
        assert_eq!(
            store
                .preview_manifest(&manifest)
                .unwrap()
                .content
                .as_deref(),
            Some(content)
        );
        fs::write(root.join(storage_key), "tampered").unwrap();
        assert!(store
            .preview_manifest(&manifest)
            .unwrap_err()
            .starts_with("attachment_changed"));
        fs::write(root.join(storage_key), content).unwrap();
        manifest.artifacts[1].byte_size += 1;
        assert!(store
            .preview_manifest(&manifest)
            .unwrap_err()
            .starts_with("attachment_changed"));
        manifest.artifacts[1].byte_size -= 1;
        fs::write(root.join(&manifest.storage_key), b"changed original").unwrap();
        assert!(store
            .preview_manifest(&manifest)
            .unwrap_err()
            .starts_with("attachment_changed"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn batch_total_limit_detects_valid_documents_in_any_position() {
        let root = test_root();
        fs::create_dir_all(&root).unwrap();
        let text = root.join("notes.txt");
        let document = root.join("paper.pdf");
        let invalid = root.join("invalid.docx");
        fs::write(&text, b"notes").unwrap();
        fs::write(&document, b"%PDF-1.7").unwrap();
        fs::write(&invalid, b"binary").unwrap();
        assert_eq!(batch_total_limit(&[text.clone(), invalid]), MAX_TOTAL_BYTES);
        assert_eq!(
            batch_total_limit(&[text.clone(), document.clone()]),
            MAX_DOCUMENT_TOTAL_BYTES
        );
        assert_eq!(
            batch_total_limit(&[document.clone(), text.clone()]),
            MAX_DOCUMENT_TOTAL_BYTES
        );
        let mut overflow = vec![text; MAX_ATTACHMENTS];
        overflow.push(document);
        assert_eq!(batch_total_limit(&overflow), MAX_TOTAL_BYTES);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn ordinary_files_retain_the_twenty_five_mib_total_limit() {
        let root = test_root();
        fs::create_dir_all(&root).unwrap();
        let source = root.join("small.txt");
        fs::write(&source, b"ordinary").unwrap();
        let store = AttachmentStore::new(root.clone()).unwrap();
        assert_eq!(
            store
                .import_one(&source, 25 * 1024 * 1024 - 7, MAX_TOTAL_BYTES)
                .unwrap_err()
                .code,
            "attachments_too_large"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn document_limits_preflight_oversized_files_and_enforce_total_without_large_payloads() {
        let root = test_root();
        fs::create_dir_all(&root).unwrap();
        let oversized = root.join("large.pdf");
        fs::File::create(&oversized)
            .unwrap()
            .set_len(MAX_DOCUMENT_BYTES + 1)
            .unwrap();
        let source = root.join("small.pdf");
        fs::write(&source, b"%PDF-1.7").unwrap();
        let store = AttachmentStore::new(root.clone()).unwrap();
        assert_eq!(
            store
                .import_one(&oversized, 0, MAX_TOTAL_BYTES)
                .unwrap_err()
                .code,
            "attachment_too_large"
        );
        assert!(store
            .import_one(&source, MAX_DOCUMENT_TOTAL_BYTES - 8, MAX_TOTAL_BYTES)
            .is_ok());
        assert_eq!(
            store
                .import_one(&source, MAX_DOCUMENT_TOTAL_BYTES - 7, MAX_TOTAL_BYTES)
                .unwrap_err()
                .code,
            "attachments_too_large"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn clipboard_png_uses_normal_staging_preview_and_cleanup() {
        let root = test_root();
        let mut store = AttachmentStore::new(root.clone()).unwrap();
        let mut bytes = std::io::Cursor::new(Vec::new());
        image::DynamicImage::ImageRgba8(image::ImageBuffer::from_pixel(
            16,
            8,
            image::Rgba([30, 40, 50, 255]),
        ))
        .write_to(&mut bytes, image::ImageFormat::Png)
        .unwrap();
        let result =
            store.import_clipboard_image("../../capture.png", &STANDARD.encode(bytes.get_ref()));
        assert!(result.rejected.is_empty());
        let manifest = &result.accepted[0];
        assert_eq!(manifest.name, "capture.png");
        assert_eq!((manifest.width, manifest.height), (Some(16), Some(8)));
        assert_eq!(
            fs::read(root.join(&manifest.storage_key)).unwrap(),
            *bytes.get_ref()
        );
        assert!(store
            .preview(&manifest.id)
            .unwrap()
            .data_url
            .unwrap()
            .starts_with("data:image/png;base64,"));
        assert_eq!(
            store
                .resolve(std::slice::from_ref(&manifest.id))
                .unwrap()
                .len(),
            1
        );
        assert!(!fs::read_dir(&store.root).unwrap().any(|entry| entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .contains("clipboard")));
        store.discard(&manifest.id).unwrap();
        assert_eq!(fs::read_dir(&store.root).unwrap().count(), 0);
        assert!(root
            .canonicalize()
            .unwrap()
            .starts_with(std::env::temp_dir().canonicalize().unwrap()));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn clipboard_rejects_malformed_oversized_and_non_image_inputs_without_files() {
        let root = test_root();
        let mut store = AttachmentStore::new(root.clone()).unwrap();
        let oversized = "A".repeat((MAX_IMAGE_BYTES.div_ceil(3) * 4 + 1) as usize);
        for (name, data, code) in [
            (
                "image.png",
                "not base64!".to_string(),
                "attachment_processing_failed",
            ),
            ("image.png", oversized, "attachment_too_large"),
            (
                "notes.txt",
                STANDARD.encode(b"not an image"),
                "attachment_type_unsupported",
            ),
            (
                "image.png",
                STANDARD.encode(b"not an image"),
                "attachment_signature_mismatch",
            ),
            (
                "broken.png",
                STANDARD.encode(b"\x89PNG\r\n\x1a\nbroken"),
                "image_normalization_failed",
            ),
        ] {
            let result = store.import_clipboard_image(name, &data);
            assert!(result.accepted.is_empty());
            assert_eq!(result.rejected[0].error_code, code);
            store.discard(&result.rejected[0].receipt_id).unwrap();
        }
        assert_eq!(fs::read_dir(&store.root).unwrap().count(), 0);
        assert!(root
            .canonicalize()
            .unwrap()
            .starts_with(std::env::temp_dir().canonicalize().unwrap()));
        fs::remove_dir_all(root).unwrap();
    }

    fn test_root() -> PathBuf {
        std::env::temp_dir().join(format!("agent-attachment-test-{}", uuid::Uuid::new_v4()))
    }

    #[test]
    fn imports_utf8_snapshot_and_rejects_unknown_types() {
        let root = test_root();
        fs::create_dir_all(&root).unwrap();
        let source = root.join("notes.md");
        fs::write(&source, "hello attachment").unwrap();
        let mut store = AttachmentStore::new(root.clone()).unwrap();
        let imported = store.import_paths(&[source]);
        assert_eq!(imported.accepted[0].kind, "text");
        let preview = store.preview(&imported.accepted[0].id).unwrap();
        assert_eq!(preview.content.as_deref(), Some("hello attachment"));
        let unsupported = root.join("payload.exe");
        fs::write(&unsupported, b"not executable").unwrap();
        let result = store.import_paths(&[unsupported]);
        assert!(result.accepted.is_empty());
        assert_eq!(result.rejected[0].error_code, "attachment_type_unsupported");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn rejects_dropped_directories_without_recursing() {
        let root = test_root();
        let dropped = root.join("folder");
        fs::create_dir_all(&dropped).unwrap();
        fs::write(dropped.join("inside.txt"), "private").unwrap();
        let mut store = AttachmentStore::new(root.clone()).unwrap();
        let result = store.import_paths(&[dropped]);
        assert!(result.accepted.is_empty());
        assert_eq!(
            result.rejected[0].error_code,
            "attachment_directory_unsupported"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn detects_snapshot_tampering_before_preview() {
        let root = test_root();
        fs::create_dir_all(&root).unwrap();
        let source = root.join("notes.txt");
        fs::write(&source, "original").unwrap();
        let mut store = AttachmentStore::new(root.clone()).unwrap();
        let manifest = store.import_paths(&[source]).accepted.remove(0);
        fs::write(root.join(&manifest.storage_key), "changed").unwrap();
        assert!(store
            .preview_manifest(&manifest)
            .unwrap_err()
            .starts_with("attachment_changed"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn image_import_creates_and_discards_both_snapshots() {
        let root = test_root();
        fs::create_dir_all(&root).unwrap();
        let source = root.join("sample.png");
        image::ImageBuffer::from_pixel(24, 12, image::Rgba([30_u8, 40, 50, 120]))
            .save(&source)
            .unwrap();
        let mut store = AttachmentStore::new(root.clone()).unwrap();
        let manifest = store.import_paths(&[source]).accepted.remove(0);
        assert_eq!(
            manifest.normalization_version.as_deref(),
            Some("image-normalization-v1")
        );
        assert_eq!(manifest.model_media_type.as_deref(), Some("image/png"));
        let original = root.join(&manifest.storage_key);
        let model = root.join(manifest.model_storage_key.as_deref().unwrap());
        assert!(original.exists());
        assert!(model.exists());
        assert_eq!(store.normalization_status(&manifest).status, "ready");
        store.discard(&manifest.id).unwrap();
        assert!(!original.exists());
        assert!(!model.exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn legacy_image_gets_a_model_copy_on_first_retry() {
        let root = test_root();
        fs::create_dir_all(&root).unwrap();
        let source = root.join("legacy.webp");
        image::ImageBuffer::from_pixel(18, 9, image::Rgba([20_u8, 40, 80, 160]))
            .save(&source)
            .unwrap();
        let mut store = AttachmentStore::new(root.clone()).unwrap();
        let mut manifest = store.import_paths(&[source]).accepted.remove(0);
        fs::remove_file(root.join(manifest.model_storage_key.as_deref().unwrap())).unwrap();
        manifest.model_storage_key = None;
        manifest.model_media_type = None;
        manifest.model_byte_size = None;
        manifest.model_sha256 = None;
        manifest.model_width = None;
        manifest.model_height = None;
        manifest.normalization_version = Some("legacy_passthrough".to_string());

        let normalized = store.ensure_model_copy(&manifest).unwrap();
        assert_eq!(
            normalized.normalization_version.as_deref(),
            Some("image-normalization-v1")
        );
        assert!(root
            .join(normalized.model_storage_key.as_deref().unwrap())
            .exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn mixed_batch_keeps_valid_files_and_returns_rejection_receipts() {
        let root = test_root();
        fs::create_dir_all(&root).unwrap();
        let valid = root.join("notes.txt");
        let invalid = root.join("paper.bin");
        fs::write(&valid, "safe text").unwrap();
        fs::write(&invalid, b"%PDF-1.7").unwrap();
        let mut store = AttachmentStore::new(root.clone()).unwrap();

        let result = store.import_paths(&[valid, invalid]);

        assert_eq!(result.accepted.len(), 1);
        assert_eq!(result.rejected.len(), 1);
        assert_eq!(
            result.rejected[0].detected_media_type.as_deref(),
            Some("application/pdf")
        );
        assert!(store.preview(&result.accepted[0].id).is_ok());
        assert!(store
            .resolve_rejections(&[result.rejected[0].receipt_id.clone()])
            .is_ok());
        fs::remove_dir_all(root).unwrap();
    }
}
