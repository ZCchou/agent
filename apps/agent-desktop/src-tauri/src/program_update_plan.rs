//! Versioned updater handoff shared by the Host and standalone helper.
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

pub fn read_json(path: &Path, maximum: u64) -> Result<serde_json::Value, String> {
    if crate::program_update::file_identity(path)?.bytes > maximum {
        return Err("app_update_handoff_invalid".into());
    }
    serde_json::from_slice(&std::fs::read(path).map_err(|_| "app_update_handoff_invalid")?)
        .map_err(|_| "app_update_handoff_invalid".into())
}

pub fn write_json(path: &Path, value: &impl Serialize) -> Result<(), String> {
    use std::io::Write;
    let bytes = serde_json::to_vec(value).map_err(|_| "app_update_install_failed")?;
    let temporary = path.with_extension(format!("{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| {
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .map_err(|_| "app_update_install_failed")?;
        file.write_all(&bytes)
            .map_err(|_| "app_update_install_failed")?;
        file.sync_all().map_err(|_| "app_update_install_failed")?;
        drop(file);
        publish_new(&temporary, path)
    })();
    let _ = std::fs::remove_file(temporary);
    result
}

#[cfg(windows)]
fn publish_new(source: &Path, target: &Path) -> Result<(), String> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{MoveFileExW, MOVEFILE_WRITE_THROUGH};
    let source: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let target: Vec<u16> = target.as_os_str().encode_wide().chain(Some(0)).collect();
    if unsafe { MoveFileExW(source.as_ptr(), target.as_ptr(), MOVEFILE_WRITE_THROUGH) } == 0 {
        return Err("app_update_install_failed".into());
    }
    Ok(())
}

#[cfg(not(windows))]
fn publish_new(source: &Path, target: &Path) -> Result<(), String> {
    std::fs::hard_link(source, target).map_err(|_| "app_update_install_failed")?;
    std::fs::remove_file(source).map_err(|_| "app_update_install_failed".into())
}

pub fn replace_json(path: &Path, value: &serde_json::Value) -> Result<(), String> {
    let temporary = path.with_extension(format!("{}.tmp", uuid::Uuid::new_v4()));
    let result = write_json(&temporary, value).and_then(|_| {
        std::fs::rename(&temporary, path).map_err(|_| "app_update_install_failed".into())
    });
    let _ = std::fs::remove_file(temporary);
    result
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Handoff {
    pub schema_version: u32,
    pub application_id: String,
    pub version: String,
    pub nonce: String,
    pub parent_pid: u32,
    pub data_root: PathBuf,
}

pub fn validate_handoff(
    value: &serde_json::Value,
    install: &Path,
    transaction: &Path,
) -> Result<Handoff, String> {
    let plan: Handoff =
        serde_json::from_value(value.clone()).map_err(|_| "app_update_handoff_invalid")?;
    let identity = uuid::Uuid::parse_str(&plan.nonce).map_err(|_| "app_update_handoff_invalid")?;
    let install = install
        .canonicalize()
        .map_err(|_| "app_update_handoff_invalid")?;
    let resolved_transaction = transaction
        .canonicalize()
        .map_err(|_| "app_update_handoff_invalid")?;
    if plan.schema_version != 1
        || plan.application_id != crate::program_update::APPLICATION_ID
        || plan.parent_pid == 0
        || identity.to_string() != plan.nonce
        || semver::Version::parse(&plan.version).is_err()
        || transaction.file_name().and_then(|name| name.to_str()) != Some(plan.nonce.as_str())
        || transaction
            .parent()
            .and_then(Path::file_name)
            .and_then(|name| name.to_str())
            != Some(".agent-program-updates")
        || resolved_transaction != install.join(".agent-program-updates").join(&plan.nonce)
        || !plan.data_root.is_absolute()
        || plan.data_root.file_name().and_then(|name| name.to_str())
            != Some(crate::program_update::APPLICATION_ID)
    {
        return Err("app_update_handoff_invalid".into());
    }
    Ok(plan)
}

pub fn validate_health(
    value: &serde_json::Value,
    plan: &Handoff,
    contract: &serde_json::Value,
) -> Result<(), String> {
    if value["application_id"] != plan.application_id
        || value["version"] != plan.version
        || value["nonce"] != plan.nonce
        || value["core"]["core_version"] != plan.version
        || contract["schema_version"]
            .as_u64()
            .is_none_or(|number| number == 0)
        || contract["protocol_version"]
            .as_u64()
            .is_none_or(|number| number == 0)
        || contract["build_id"]
            .as_str()
            .is_none_or(|value| value.is_empty() || value.len() > 128)
        || ["schema_version", "protocol_version", "build_id"]
            .iter()
            .any(|field| value["core"][*field] != contract[*field])
    {
        return Err("app_update_health_failed".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failed_journal_serialization_never_publishes_a_partial_record() {
        struct Interrupted;
        impl serde::Serialize for Interrupted {
            fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                use serde::ser::SerializeSeq;
                let mut sequence = serializer.serialize_seq(None)?;
                sequence.serialize_element("partially serialized")?;
                Err(serde::ser::Error::custom("simulated interruption"))
            }
        }
        let root = std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());
        std::fs::create_dir(&root).unwrap();
        for name in [
            ".agent-update-pending.json",
            "pending.json",
            "data-backup.json",
        ] {
            let path = root.join(name);
            assert!(write_json(&path, &Interrupted).is_err());
            assert!(!path.exists());
            write_json(&path, &serde_json::json!({"complete":true})).unwrap();
            assert!(write_json(&path, &serde_json::json!({"overwrite":true})).is_err());
            assert_eq!(
                read_json(&path, 1024).unwrap(),
                serde_json::json!({"complete":true})
            );
        }
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn handoff_is_bound_to_the_application_and_exact_transaction() {
        let root = std::env::temp_dir().join(format!("agent-handoff-{}", uuid::Uuid::new_v4()));
        let id = uuid::Uuid::new_v4().to_string();
        let transaction = root.join(".agent-program-updates").join(&id);
        std::fs::create_dir_all(&transaction).unwrap();
        let value = serde_json::json!({"schema_version":1,"application_id":crate::program_update::APPLICATION_ID,
            "version":"0.16.0-preview.2","nonce":id,"parent_pid":std::process::id(),
            "data_root":root.join(crate::program_update::APPLICATION_ID)});
        let plan = validate_handoff(&value, &root, &transaction).unwrap();
        assert_eq!(plan.nonce, id);
        let mut wrong = value.clone();
        wrong["application_id"] = "punkagent".into();
        assert!(validate_handoff(&wrong, &root, &transaction).is_err());
        wrong = value.clone();
        wrong["nonce"] = uuid::Uuid::new_v4().to_string().into();
        assert!(validate_handoff(&wrong, &root, &transaction).is_err());
        assert!(validate_handoff(&value, &root.join("other"), &transaction).is_err());
        std::fs::remove_dir_all(root).unwrap();
        assert!(validate_handoff(
            &value,
            transaction.parent().unwrap().parent().unwrap(),
            &transaction
        )
        .is_err());
    }

    #[test]
    fn health_requires_matching_nonce_version_and_core_contract() {
        let plan = Handoff {
            schema_version: 1,
            application_id: crate::program_update::APPLICATION_ID.into(),
            version: "0.16.0-preview.2".into(),
            nonce: uuid::Uuid::new_v4().to_string(),
            parent_pid: 1,
            data_root: "C:/profile".into(),
        };
        let contract = serde_json::json!({"schema_version":30,"protocol_version":23,"build_id":"schema30-agent-unified-v1"});
        let response = serde_json::json!({"application_id":plan.application_id,"version":plan.version,"nonce":plan.nonce,
            "core":{"core_version":plan.version,"schema_version":30,"protocol_version":23,"build_id":"schema30-agent-unified-v1"}});
        assert!(validate_health(&response, &plan, &contract).is_ok());
        let mut wrong = response.clone();
        wrong["nonce"] = "other".into();
        assert!(validate_health(&wrong, &plan, &contract).is_err());
        wrong = response.clone();
        wrong["core"]["schema_version"] = 29.into();
        assert!(validate_health(&wrong, &plan, &contract).is_err());
        wrong = response.clone();
        wrong["version"] = "0.16.0-preview.1".into();
        assert!(validate_health(&wrong, &plan, &contract).is_err());
    }
}
