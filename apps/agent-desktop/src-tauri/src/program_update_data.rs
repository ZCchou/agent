//! Database and configuration recovery for a stopped, application-owned profile.
use crate::program_update::{file_identity, FileIdentity, APPLICATION_ID};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

const DATA_FILES: [&str; 7] = [
    "agent.db",
    "settings.json",
    "work-tool-grants.json",
    "skills.json",
    "rag/rag-models.json",
    "rag/ocr-storage.json",
    "route-health-v2.json",
];

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct DataBackup {
    application_id: String,
    root: PathBuf,
    files: BTreeMap<String, Option<FileIdentity>>,
}

fn checked_child(root: &Path, relative: &str) -> Result<PathBuf, String> {
    let mut current = root.to_path_buf();
    for component in Path::new(relative).components() {
        if !matches!(component, std::path::Component::Normal(_)) {
            return Err("app_update_data_invalid".into());
        }
        current.push(component);
        if current.exists() {
            let metadata = fs::symlink_metadata(&current).map_err(|_| "app_update_data_invalid")?;
            if metadata.file_type().is_symlink() {
                return Err("app_update_data_invalid".into());
            }
            #[cfg(windows)]
            {
                use std::os::windows::fs::MetadataExt;
                if metadata.file_attributes() & 0x400 != 0 {
                    return Err("app_update_data_invalid".into());
                }
            }
        }
    }
    Ok(current)
}

fn data_root(root: &Path) -> Result<PathBuf, String> {
    let root = root.canonicalize().map_err(|_| "app_update_data_invalid")?;
    if root.file_name().and_then(|name| name.to_str()) != Some(APPLICATION_ID) || !root.is_dir() {
        return Err("app_update_data_invalid".into());
    }
    Ok(root)
}

fn matches(path: &Path, expected: &FileIdentity) -> bool {
    file_identity(path).is_ok_and(|identity| {
        identity.bytes == expected.bytes && identity.sha256 == expected.sha256
    })
}

fn copy_verified(source: &Path, destination: &Path, identity: &FileIdentity) -> Result<(), String> {
    let parent = destination.parent().ok_or("app_update_data_invalid")?;
    fs::create_dir_all(parent).map_err(|_| "app_update_install_failed")?;
    let temporary = parent.join(format!(".update-data-{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| {
        fs::copy(source, &temporary).map_err(|_| "app_update_install_failed")?;
        fs::OpenOptions::new()
            .write(true)
            .open(&temporary)
            .and_then(|file| file.sync_all())
            .map_err(|_| "app_update_install_failed")?;
        if !matches(&temporary, identity) {
            return Err("app_update_data_invalid".into());
        }
        fs::rename(&temporary, destination).map_err(|_| "app_update_install_failed".into())
    })();
    let _ = fs::remove_file(temporary);
    result
}

pub fn backup_profile(root: &Path, transaction: &Path) -> Result<(), String> {
    let root = data_root(root)?;
    if ["agent.db-wal", "agent.db-shm", "agent.db-journal"]
        .iter()
        .any(|name| root.join(name).exists())
    {
        return Err("app_update_busy".into());
    }
    let previous = transaction.join("previous-data");
    fs::create_dir(&previous).map_err(|_| "app_update_install_failed")?;
    let mut files = BTreeMap::new();
    for name in DATA_FILES {
        let source = checked_child(&root, name)?;
        let identity = if source.exists() {
            let identity = file_identity(&source)?;
            copy_verified(&source, &checked_child(&previous, name)?, &identity)?;
            Some(identity)
        } else {
            None
        };
        files.insert(name.into(), identity);
    }
    let record = DataBackup {
        application_id: APPLICATION_ID.into(),
        root,
        files,
    };
    let path = transaction.join("data-backup.json");
    crate::program_update_plan::write_json(&path, &record)
}

pub fn restore_profile(transaction: &Path) -> Result<(), String> {
    let path = transaction.join("data-backup.json");
    if file_identity(&path)?.bytes > 1024 * 1024 {
        return Err("app_update_data_invalid".into());
    }
    let record: DataBackup =
        serde_json::from_slice(&fs::read(path).map_err(|_| "app_update_data_invalid")?)
            .map_err(|_| "app_update_data_invalid")?;
    let root = data_root(&record.root)?;
    if record.application_id != APPLICATION_ID
        || record.files.len() != DATA_FILES.len()
        || DATA_FILES
            .iter()
            .any(|name| !record.files.contains_key(*name))
    {
        return Err("app_update_data_invalid".into());
    }
    let previous = transaction.join("previous-data");
    for (name, identity) in &record.files {
        checked_child(&root, name)?;
        if let Some(identity) = identity {
            if !matches(&checked_child(&previous, name)?, identity) {
                return Err("app_update_data_invalid".into());
            }
        }
    }
    // The helper owns and has stopped the upgraded Host and its Core before restore.
    for name in ["agent.db-wal", "agent.db-shm", "agent.db-journal"] {
        let sidecar = checked_child(&root, name)?;
        if sidecar.exists() {
            fs::remove_file(sidecar).map_err(|_| "app_update_install_failed")?;
        }
    }
    for (name, identity) in &record.files {
        let destination = checked_child(&root, name)?;
        if let Some(identity) = identity {
            copy_verified(&previous.join(name), &destination, identity)?;
        } else if destination.exists() {
            fs::remove_file(destination).map_err(|_| "app_update_install_failed")?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn failed_upgrade_restores_data_and_preserves_unmanaged_files() {
        let root = std::env::temp_dir().join(format!("agent-update-data-{}", uuid::Uuid::new_v4()));
        let data = root.join(crate::program_update::APPLICATION_ID);
        let transaction = root.join("transaction");
        fs::create_dir_all(&data).unwrap();
        fs::create_dir(&transaction).unwrap();
        fs::write(data.join("agent.db"), b"old database").unwrap();
        fs::write(data.join("personal-file.txt"), b"keep").unwrap();
        backup_profile(&data, &transaction).unwrap();
        fs::write(data.join("agent.db"), b"new migrated database").unwrap();
        fs::write(data.join("agent.db-wal"), b"new WAL").unwrap();
        fs::write(data.join("settings.json"), b"new settings").unwrap();
        restore_profile(&transaction).unwrap();
        assert_eq!(fs::read(data.join("agent.db")).unwrap(), b"old database");
        assert_eq!(fs::read(data.join("personal-file.txt")).unwrap(), b"keep");
        assert!(!data.join("settings.json").exists());
        assert!(!data.join("agent.db-wal").exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn backup_refuses_live_wal_and_foreign_application_roots() {
        let root = std::env::temp_dir().join(format!("agent-update-data-{}", uuid::Uuid::new_v4()));
        let data = root.join(crate::program_update::APPLICATION_ID);
        fs::create_dir_all(&data).unwrap();
        fs::create_dir(root.join("transaction")).unwrap();
        fs::write(data.join("agent.db-wal"), b"busy").unwrap();
        assert!(backup_profile(&data, &root.join("transaction")).is_err());
        assert!(backup_profile(&root, &root.join("transaction")).is_err());
        fs::remove_dir_all(root).unwrap();
    }
}
