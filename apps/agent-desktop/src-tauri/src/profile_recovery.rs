//! Re-key historical file recovery records without persisting plaintext.
use crate::change_set_recovery::{read_encrypted, write_encrypted};
use serde_json::{json, Value};
use std::{fs, path::Path};

pub(crate) fn restore_equivalent_catalog(
    source: &Path,
    target: &Path,
    key: &[u8; 32],
    expected: &crate::program_update::FileIdentity,
) -> Result<(), String> {
    use std::io::Write;
    let identity = crate::program_update::file_identity(source)?;
    if identity.bytes != expected.bytes || identity.sha256 != expected.sha256 {
        return Err("migration_recovery_invalid".into());
    }
    let mut candidate: Value = read_encrypted(source, key)?;
    let mut current: Value = read_encrypted(target, key)?;
    for value in [&mut candidate, &mut current] {
        value
            .as_object_mut()
            .ok_or("migration_recovery_invalid")?
            .remove("last_seen_utc_ms");
    }
    if candidate != current {
        return Err("migration_recovery_conflict".into());
    }
    let temporary = target.with_extension(format!("{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .map_err(|_| "migration_storage_failed")?;
        file.write_all(&fs::read(source).map_err(|_| "migration_storage_failed")?)
            .map_err(|_| "migration_storage_failed")?;
        file.sync_all().map_err(|_| "migration_storage_failed")?;
        drop(file);
        fs::rename(&temporary, target).map_err(|_| "migration_storage_failed".into())
    })();
    let _ = fs::remove_file(temporary);
    result
}

fn remap(value: &mut Value, mapping: &Value) {
    match value {
        Value::Object(object) => {
            for (key, item) in object {
                if matches!(
                    key.as_str(),
                    "entries" | "after" | "content_base64" | "mutations"
                ) {
                    continue;
                }
                if key == "project_max_sequence" || key == "records" {
                    if let Some(items) = item.as_object_mut() {
                        let old = std::mem::take(items);
                        for (id, mut record) in old {
                            remap(&mut record, mapping);
                            items.insert(mapping[&id].as_str().unwrap_or(&id).to_owned(), record);
                        }
                    }
                } else if matches!(
                    key.as_str(),
                    "id" | "operation_id"
                        | "run_id"
                        | "conversation_id"
                        | "project_id"
                        | "recovery_point_id"
                        | "execution_id"
                ) {
                    if let Some(replacement) = item
                        .as_str()
                        .and_then(|id| mapping.get(id))
                        .and_then(Value::as_str)
                    {
                        *item = Value::String(replacement.into());
                    }
                } else {
                    remap(item, mapping);
                }
            }
        }
        Value::Array(items) => items.iter_mut().for_each(|item| remap(item, mapping)),
        _ => {}
    }
}

fn merge_catalog(target: &mut Value, source: Value) -> Result<(), String> {
    if target["version"] != source["version"] || source["version"] != 1 {
        return Err("migration_recovery_invalid".into());
    }
    for (id, record) in source["records"]
        .as_object()
        .ok_or("migration_recovery_invalid")?
    {
        let records = target["records"]
            .as_object_mut()
            .ok_or("migration_recovery_invalid")?;
        if records.get(id).is_some_and(|existing| existing != record) {
            return Err("migration_recovery_conflict".into());
        }
        records.insert(id.clone(), record.clone());
    }
    for (id, sequence) in source["project_max_sequence"]
        .as_object()
        .ok_or("migration_recovery_invalid")?
    {
        let previous = target["project_max_sequence"][id].as_u64().unwrap_or(0);
        target["project_max_sequence"][id] =
            json!(previous.max(sequence.as_u64().ok_or("migration_recovery_invalid")?));
    }
    target["last_seen_utc_ms"] = json!(target["last_seen_utc_ms"]
        .as_i64()
        .unwrap_or(0)
        .max(source["last_seen_utc_ms"].as_i64().unwrap_or(0)));
    Ok(())
}

pub(crate) fn stage_source(
    source: &Path,
    destination: &Path,
    source_key: &[u8; 32],
    target_key: &[u8; 32],
    mapping: &Value,
) -> Result<(), String> {
    if !source.exists() {
        return Ok(());
    }
    fs::create_dir_all(destination).map_err(|_| "migration_storage_failed")?;
    for entry in fs::read_dir(source).map_err(|_| "migration_recovery_invalid")? {
        let path = entry.map_err(|_| "migration_recovery_invalid")?.path();
        let metadata = fs::symlink_metadata(&path).map_err(|_| "migration_recovery_invalid")?;
        if !metadata.is_file() || metadata.file_type().is_symlink() {
            return Err("migration_recovery_invalid".into());
        }
        let name = path
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or("migration_recovery_invalid")?;
        let catalog = name == "undo-catalog-v1.bin";
        if !catalog {
            let stem = path
                .file_stem()
                .and_then(|value| value.to_str())
                .ok_or("migration_recovery_invalid")?;
            uuid::Uuid::parse_str(stem).map_err(|_| "migration_recovery_invalid")?;
            if !matches!(
                path.extension().and_then(|value| value.to_str()),
                Some("recovery" | "journal")
            ) {
                return Err("migration_recovery_invalid".into());
            }
        }
        let original: Value = read_encrypted(&path, source_key)?;
        let mut value = original.clone();
        remap(&mut value, mapping);
        let name = if catalog {
            name.to_owned()
        } else {
            let id = path.file_stem().unwrap().to_str().unwrap();
            format!(
                "{}.{}",
                mapping[id].as_str().unwrap_or(id),
                path.extension().unwrap().to_str().unwrap()
            )
        };
        let target = destination.join(name);
        if target.exists() {
            let mut existing: Value = read_encrypted(&target, target_key)?;
            if catalog {
                merge_catalog(&mut existing, value)?;
                value = existing;
            } else if existing != value {
                return Err("migration_recovery_conflict".into());
            } else {
                continue;
            }
        }
        if source_key == target_key && original == value {
            fs::copy(&path, target).map_err(|_| "migration_storage_failed")?;
        } else {
            write_encrypted(&target, target_key, &value)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::change_set_recovery::{read_encrypted, write_encrypted};
    use serde_json::json;

    #[test]
    fn rekeys_records_and_typed_references_preserving_payloads() {
        let root = std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());
        let source = root.join("source");
        let target = root.join("candidate");
        std::fs::create_dir_all(&source).unwrap();
        let id = uuid::Uuid::new_v4().to_string();
        let original =
            json!({"id":id,"project_id":"old","entries":{"old":{"content_base64":"old"}}});
        write_encrypted(&source.join(format!("{id}.recovery")), &[1; 32], &original).unwrap();
        write_encrypted(&source.join("undo-catalog-v1.bin"), &[1;32], &json!({
            "version":1,"last_seen_utc_ms":10,"project_max_sequence":{"old":2},
            "records":{id.clone():{"change_set":{"project_id":"old","title":"old"},"undo_sequence":2}}
        })).unwrap();
        stage_source(&source, &target, &[1; 32], &[2; 32], &json!({"old":"new"})).unwrap();
        let recovered: serde_json::Value =
            read_encrypted(&target.join(format!("{id}.recovery")), &[2; 32]).unwrap();
        assert_eq!(recovered["project_id"], "new");
        assert_eq!(recovered["entries"], original["entries"]);
        let catalog: serde_json::Value =
            read_encrypted(&target.join("undo-catalog-v1.bin"), &[2; 32]).unwrap();
        assert_eq!(catalog["records"][&id]["change_set"]["title"], "old");
        assert_eq!(catalog["project_max_sequence"]["new"], 2);
        assert!(read_encrypted::<serde_json::Value>(
            &target.join(format!("{id}.recovery")),
            &[1; 32]
        )
        .is_err());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn update_probe_cannot_recover_or_prune_file_history() {
        let root = std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());
        let directory = root.join("file-recovery");
        crate::agent_execution::AgentExecutionManager::for_startup(
            directory.clone(),
            "secret",
            true,
            false,
        )
        .unwrap();
        assert!(!directory.exists());
    }

    #[test]
    fn pending_migration_cannot_normalize_or_prune_recovery_files() {
        let root = std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());
        crate::agent_execution::AgentExecutionManager::for_startup(
            root.clone(),
            "secret",
            false,
            true,
        )
        .unwrap();
        assert!(!root.exists());
    }

    #[test]
    fn rollback_accepts_nonce_changes_and_preserves_actual_recovery_edits() {
        let root = std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());
        fs::create_dir(&root).unwrap();
        let candidate = root.join("candidate.bin");
        let current = root.join("current.bin");
        let original =
            json!({"version":1,"last_seen_utc_ms":1,"records":{},"project_max_sequence":{}});
        write_encrypted(&candidate, &[3; 32], &original).unwrap();
        let identity = crate::program_update::file_identity(&candidate).unwrap();
        let mut normalized = original.clone();
        normalized["last_seen_utc_ms"] = json!(2);
        write_encrypted(&current, &[3; 32], &normalized).unwrap();
        restore_equivalent_catalog(&candidate, &current, &[3; 32], &identity).unwrap();
        assert_eq!(fs::read(&candidate).unwrap(), fs::read(&current).unwrap());
        normalized["records"]["new_user_operation"] = json!({"status":"completed"});
        write_encrypted(&current, &[3; 32], &normalized).unwrap();
        let edited = fs::read(&current).unwrap();
        assert!(restore_equivalent_catalog(&candidate, &current, &[3; 32], &identity).is_err());
        assert_eq!(fs::read(&current).unwrap(), edited);
        fs::remove_dir_all(root).unwrap();
    }
}
