//! Program-only update contract shared with the offline updater helper.
use base64::Engine;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::{
    fs,
    io::{Read, Write},
    path::Path,
};

pub const APPLICATION_ID: &str = "io.github.zcchou.agent.airworthiness";
pub const PROGRAM_FILES: [&str; 4] = [
    "agent-desktop.exe",
    "agent_desktop_lib.dll",
    "agent-core.exe",
    "agent-updater.exe",
];

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FileIdentity {
    pub bytes: u64,
    pub sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CoreContract {
    pub schema_version: u64,
    pub protocol_version: u64,
    pub build_id: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProgramManifest {
    pub schema_version: u32,
    pub application_id: String,
    pub version: String,
    pub files: BTreeMap<String, FileIdentity>,
    pub runtime_manifests: BTreeMap<String, FileIdentity>,
    pub source_commit: Option<String>,
    #[serde(default)]
    pub core_contract: Option<CoreContract>,
}

pub fn validate_manifest(
    value: &serde_json::Value,
    version: &str,
) -> Result<ProgramManifest, String> {
    let manifest: ProgramManifest =
        serde_json::from_value(value.clone()).map_err(|_| "app_update_feed_invalid")?;
    if manifest.schema_version != 1
        || manifest.application_id != APPLICATION_ID
        || manifest.version != version
        || manifest.files.len() != PROGRAM_FILES.len()
        || PROGRAM_FILES
            .iter()
            .any(|name| !manifest.files.contains_key(*name))
    {
        return Err("app_update_feed_invalid".into());
    }
    if manifest.core_contract.as_ref().is_some_and(|contract| {
        contract.schema_version == 0
            || contract.protocol_version == 0
            || contract.build_id.is_empty()
            || contract.build_id.len() > 128
            || !contract
                .build_id
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    }) {
        return Err("app_update_feed_invalid".into());
    }
    let runtime_paths = [
        "llm-runtime/runtime.json",
        "rag-runtime/runtime.json",
        "ocr-runtime/runtime.json",
        "ocr-model-package/ocr-model.json",
        "office-runtime/runtime.json",
    ];
    if manifest
        .runtime_manifests
        .keys()
        .any(|path| !runtime_paths.contains(&path.as_str()))
    {
        return Err("app_update_feed_invalid".into());
    }
    for identity in manifest
        .files
        .values()
        .chain(manifest.runtime_manifests.values())
    {
        if identity.bytes == 0
            || identity.bytes > 256 * 1024 * 1024
            || identity.sha256.len() != 64
            || !identity
                .sha256
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Err("app_update_feed_invalid".into());
        }
    }
    Ok(manifest)
}

pub fn allowed_entry(path: &str) -> bool {
    path == "update.json"
        || PROGRAM_FILES
            .iter()
            .any(|name| path == format!("payload/{name}"))
}

pub fn verify_signed_bytes(bytes: &[u8], signature: &[u8], public_key: &str) -> Result<(), String> {
    let decode = |value: &[u8]| -> Result<String, String> {
        let compact: Vec<u8> = value
            .iter()
            .copied()
            .filter(|byte| !byte.is_ascii_whitespace())
            .collect();
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(compact)
            .map_err(|_| "app_update_signature_invalid")?;
        String::from_utf8(decoded).map_err(|_| "app_update_signature_invalid".into())
    };
    let key = minisign_verify::PublicKey::decode(&decode(public_key.as_bytes())?)
        .map_err(|_| "app_update_signature_invalid")?;
    let signature = minisign_verify::Signature::decode(&decode(signature)?)
        .map_err(|_| "app_update_signature_invalid")?;
    key.verify(bytes, &signature, true)
        .map_err(|_| "app_update_signature_invalid".into())
}

pub fn stage_archive(
    bytes: &[u8],
    version: &str,
    destination: &Path,
) -> Result<ProgramManifest, String> {
    if bytes.len() > 512 * 1024 * 1024 || destination.exists() {
        return Err("app_update_feed_invalid".into());
    }
    let mut archive =
        zip::ZipArchive::new(std::io::Cursor::new(bytes)).map_err(|_| "app_update_feed_invalid")?;
    let mut names = std::collections::HashSet::new();
    if archive.len() != PROGRAM_FILES.len() + 1 {
        return Err("app_update_feed_invalid".into());
    }
    for index in 0..archive.len() {
        let entry = archive
            .by_index(index)
            .map_err(|_| "app_update_feed_invalid")?;
        let kind = entry.unix_mode().unwrap_or(0) & 0o170000;
        if !allowed_entry(entry.name())
            || !names.insert(entry.name().to_owned())
            || !matches!(kind, 0 | 0o100000)
            || entry.size() > 256 * 1024 * 1024
        {
            return Err("app_update_feed_invalid".into());
        }
    }
    let manifest = {
        let entry = archive
            .by_name("update.json")
            .map_err(|_| "app_update_feed_invalid")?;
        if entry.size() > 1024 * 1024 {
            return Err("app_update_feed_invalid".into());
        }
        let value: serde_json::Value =
            serde_json::from_reader(entry).map_err(|_| "app_update_feed_invalid")?;
        validate_manifest(&value, version)?
    };
    fs::create_dir(destination).map_err(|_| "app_update_install_failed")?;
    let result = (|| {
        for (name, identity) in &manifest.files {
            let mut entry = archive
                .by_name(&format!("payload/{name}"))
                .map_err(|_| "app_update_feed_invalid")?;
            if entry.size() != identity.bytes {
                return Err("app_update_feed_invalid".to_string());
            }
            let mut output = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(destination.join(name))
                .map_err(|_| "app_update_install_failed")?;
            let mut digest = Sha256::new();
            let mut count = 0_u64;
            let mut buffer = [0_u8; 64 * 1024];
            loop {
                let size = entry
                    .read(&mut buffer)
                    .map_err(|_| "app_update_feed_invalid")?;
                if size == 0 {
                    break;
                }
                count += size as u64;
                if count > identity.bytes {
                    return Err("app_update_feed_invalid".to_string());
                }
                digest.update(&buffer[..size]);
                output
                    .write_all(&buffer[..size])
                    .map_err(|_| "app_update_install_failed")?;
            }
            output.sync_all().map_err(|_| "app_update_install_failed")?;
            if count != identity.bytes || format!("{:x}", digest.finalize()) != identity.sha256 {
                return Err("app_update_signature_invalid".to_string());
            }
        }
        Ok(())
    })();
    if let Err(error) = result {
        let _ = fs::remove_dir_all(destination);
        return Err(error);
    }
    Ok(manifest)
}

fn regular_file(path: &Path) -> Result<(), String> {
    let metadata = fs::symlink_metadata(path).map_err(|_| "app_update_install_failed")?;
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return Err("app_update_install_failed".into());
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if metadata.file_attributes() & 0x400 != 0 {
            return Err("app_update_install_failed".into());
        }
    }
    Ok(())
}

pub fn file_identity(path: &Path) -> Result<FileIdentity, String> {
    regular_file(path)?;
    let mut input = fs::File::open(path).map_err(|_| "app_update_install_failed")?;
    let mut digest = Sha256::new();
    let mut bytes = 0;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let size = input
            .read(&mut buffer)
            .map_err(|_| "app_update_install_failed")?;
        if size == 0 {
            break;
        }
        bytes += size as u64;
        digest.update(&buffer[..size]);
    }
    Ok(FileIdentity {
        bytes,
        sha256: format!("{:x}", digest.finalize()),
    })
}

fn matches_file(path: &Path, expected: &FileIdentity) -> bool {
    file_identity(path)
        .is_ok_and(|actual| actual.bytes == expected.bytes && actual.sha256 == expected.sha256)
}

pub fn validate_upgrade(
    install: &Path,
    current_version: &str,
    manifest: &ProgramManifest,
) -> Result<(), String> {
    let current = semver::Version::parse(current_version).map_err(|_| "app_update_feed_invalid")?;
    let incoming =
        semver::Version::parse(&manifest.version).map_err(|_| "app_update_feed_invalid")?;
    if incoming <= current {
        return Err("app_update_stale".into());
    }
    if manifest.runtime_manifests.is_empty()
        || manifest
            .runtime_manifests
            .iter()
            .any(|(name, identity)| !matches_file(&install.join(name), identity))
    {
        return Err("app_update_runtime_incompatible".into());
    }
    Ok(())
}

fn write_record(path: &Path, value: &impl Serialize) -> Result<(), String> {
    crate::program_update_plan::write_json(path, value)
}

#[derive(Serialize, Deserialize)]
struct ProgramJournal {
    base_files: BTreeMap<String, FileIdentity>,
    manifest: ProgramManifest,
}

pub fn restore_program(install: &Path, transaction: &Path) -> Result<(), String> {
    let journal: ProgramJournal = serde_json::from_reader(
        fs::File::open(transaction.join("pending.json"))
            .map_err(|_| "app_update_install_failed")?,
    )
    .map_err(|_| "app_update_install_failed")?;
    validate_manifest(
        &serde_json::to_value(&journal.manifest).map_err(|_| "app_update_install_failed")?,
        &journal.manifest.version,
    )?;
    for name in PROGRAM_FILES {
        let old = journal
            .base_files
            .get(name)
            .ok_or("app_update_install_failed")?;
        if !matches_file(&transaction.join("previous").join(name), old) {
            return Err("app_update_signature_invalid".into());
        }
        let active = install.join(name);
        if active.exists()
            && !matches_file(&active, old)
            && !matches_file(&active, &journal.manifest.files[name])
        {
            return Err("app_update_stale".into());
        }
    }
    for name in PROGRAM_FILES.into_iter().chain(["application.json"]) {
        let restore = transaction.join(format!("restore-{name}"));
        fs::copy(transaction.join("previous").join(name), &restore)
            .map_err(|_| "app_update_install_failed")?;
        fs::rename(restore, install.join(name)).map_err(|_| "app_update_install_failed")?;
    }
    fs::rename(
        transaction.join("pending.json"),
        transaction.join("rolled-back.json"),
    )
    .map_err(|_| "app_update_install_failed".into())
}

pub fn publish_program(
    install: &Path,
    transaction: &Path,
    manifest: &ProgramManifest,
    health_check: impl FnOnce() -> Result<(), String>,
) -> Result<(), String> {
    let owner = install.join("application.json");
    regular_file(&owner)?;
    let identity: serde_json::Value =
        serde_json::from_reader(fs::File::open(&owner).map_err(|_| "app_update_install_failed")?)
            .map_err(|_| "app_update_install_failed")?;
    if identity["application_id"] != APPLICATION_ID
        || transaction
            .parent()
            .and_then(Path::file_name)
            .and_then(|name| name.to_str())
            != Some(".agent-program-updates")
        || transaction
            .parent()
            .and_then(Path::parent)
            .and_then(|parent| parent.canonicalize().ok())
            != install.canonicalize().ok()
    {
        return Err("app_update_feed_invalid".into());
    }
    let previous = transaction.join("previous");
    fs::create_dir(&previous).map_err(|_| "app_update_install_failed")?;
    let mut base_files = BTreeMap::new();
    for name in PROGRAM_FILES {
        if !matches_file(
            &transaction.join("candidate").join(name),
            &manifest.files[name],
        ) {
            return Err("app_update_signature_invalid".into());
        }
        let old = file_identity(&install.join(name))?;
        fs::copy(install.join(name), previous.join(name))
            .map_err(|_| "app_update_install_failed")?;
        if !matches_file(&previous.join(name), &old) {
            return Err("app_update_signature_invalid".into());
        }
        base_files.insert(name.to_owned(), old);
    }
    fs::copy(&owner, previous.join("application.json")).map_err(|_| "app_update_install_failed")?;
    write_record(
        &transaction.join("pending.json"),
        &ProgramJournal {
            base_files,
            manifest: manifest.clone(),
        },
    )?;
    let result: Result<(), String> = (|| {
        for name in PROGRAM_FILES {
            fs::rename(transaction.join("candidate").join(name), install.join(name))
                .map_err(|_| "app_update_install_failed")?;
        }
        let next_identity = transaction.join("application.next.json");
        write_record(&next_identity, manifest)?;
        fs::rename(next_identity, &owner).map_err(|_| "app_update_install_failed")?;
        health_check()?;
        fs::rename(
            transaction.join("pending.json"),
            transaction.join("completed.json"),
        )
        .map_err(|_| "app_update_install_failed".into())
    })();
    if let Err(error) = result {
        if matches!(
            error.as_str(),
            "app_update_process_tree_unavailable" | "app_update_data_restore_failed"
        ) {
            return Err(error);
        }
        restore_program(install, transaction)?;
        return Err(error);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn upgrade_rejects_downgrades_and_incompatible_runtime_files() {
        let root = std::env::temp_dir().join(format!("agent-upgrade-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(root.join("llm-runtime")).unwrap();
        fs::write(root.join("llm-runtime/runtime.json"), b"runtime").unwrap();
        let manifest = ProgramManifest {
            schema_version: 1,
            application_id: APPLICATION_ID.into(),
            version: "0.16.0-preview.2".into(),
            files: BTreeMap::new(),
            runtime_manifests: BTreeMap::from([(
                "llm-runtime/runtime.json".into(),
                file_identity(&root.join("llm-runtime/runtime.json")).unwrap(),
            )]),
            source_commit: None,
            core_contract: None,
        };
        assert!(validate_upgrade(&root, "0.16.0-preview.1", &manifest).is_ok());
        assert!(validate_upgrade(&root, "0.16.0-preview.2", &manifest).is_err());
        assert!(validate_upgrade(&root, "0.17.0", &manifest).is_err());
        fs::write(root.join("llm-runtime/runtime.json"), b"changed").unwrap();
        assert_eq!(
            validate_upgrade(&root, "0.16.0-preview.1", &manifest).unwrap_err(),
            "app_update_runtime_incompatible"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn program_manifest_rejects_other_application_and_model_payloads() {
        let manifest = serde_json::json!({
            "schema_version": 1, "application_id": "io.github.zcchou.agent.airworthiness",
            "version": "0.16.0-preview.2", "files": {
                "agent-desktop.exe": {"bytes": 3, "sha256": "a".repeat(64)},
                "agent_desktop_lib.dll": {"bytes": 3, "sha256": "b".repeat(64)},
                "agent-core.exe": {"bytes": 3, "sha256": "c".repeat(64)},
                "agent-updater.exe": {"bytes": 3, "sha256": "d".repeat(64)}
            }, "runtime_manifests": {}});
        assert!(validate_manifest(&manifest, "0.16.0-preview.2").is_ok());
        let mut wrong = manifest.clone();
        wrong["application_id"] = "io.github.punkagent".into();
        assert!(validate_manifest(&wrong, "0.16.0-preview.2").is_err());
        let mut weights = manifest.clone();
        weights["files"]["model.gguf"] = serde_json::json!({"bytes": 3, "sha256": "e".repeat(64)});
        assert!(validate_manifest(&weights, "0.16.0-preview.2").is_err());
        assert!(validate_manifest(&manifest, "0.16.0-preview.3").is_err());
        let mut malformed = manifest.clone();
        malformed["core_contract"] =
            serde_json::json!({"schema_version":0,"protocol_version":23,"build_id":""});
        assert!(validate_manifest(&malformed, "0.16.0-preview.2").is_err());
    }

    #[test]
    fn zip_paths_cannot_escape_program_payload_or_name_duplicates() {
        assert!(allowed_entry("update.json"));
        assert!(allowed_entry("payload/agent-core.exe"));
        for path in [
            "../agent-core.exe",
            "payload/../agent-core.exe",
            "C:/agent-core.exe",
            "payload\\agent-core.exe",
            "payload/agent-core.exe:ads",
            "payload/model.gguf",
        ] {
            assert!(!allowed_entry(path), "{path}");
        }
    }

    #[test]
    fn signature_is_verified_before_any_archive_content_is_trusted() {
        let message = include_bytes!("app_updates/fixtures/feed.json");
        let signature = include_bytes!("app_updates/fixtures/feed.json.sig");
        let key = include_str!("app_updates/fixtures/test.pub");
        assert!(verify_signed_bytes(message, signature, key).is_ok());
        assert!(verify_signed_bytes(b"tampered", signature, key).is_err());
        assert!(verify_signed_bytes(message, b"invalid", key).is_err());
    }

    fn package(corrupt: bool, extra: bool) -> Vec<u8> {
        use sha2::{Digest, Sha256};
        use std::io::{Cursor, Write};
        let digest = format!("{:x}", Sha256::digest(b"exe"));
        let files: BTreeMap<_, _> = PROGRAM_FILES
            .iter()
            .map(|name| (*name, serde_json::json!({"bytes": 3, "sha256": digest})))
            .collect();
        let manifest = serde_json::json!({"schema_version": 1, "application_id": APPLICATION_ID,
            "version": "0.16.0-preview.2", "files": files, "runtime_manifests": {}});
        let mut zip = zip::ZipWriter::new(Cursor::new(Vec::new()));
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Stored);
        zip.start_file("update.json", options).unwrap();
        zip.write_all(&serde_json::to_vec(&manifest).unwrap())
            .unwrap();
        for name in PROGRAM_FILES {
            zip.start_file(format!("payload/{name}"), options).unwrap();
            zip.write_all(if corrupt { b"bad" } else { b"exe" })
                .unwrap();
        }
        if extra {
            zip.start_file("../escape", options).unwrap();
            zip.write_all(b"outside").unwrap();
        }
        zip.finish().unwrap().into_inner()
    }

    #[test]
    fn archive_staging_checks_every_hash_and_cleans_partial_results() {
        let root = std::env::temp_dir().join(format!("agent-update-test-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap();
        let stage = root.join("candidate");
        assert!(stage_archive(&package(true, false), "0.16.0-preview.2", &stage).is_err());
        assert!(!stage.exists());
        assert!(stage_archive(&package(false, true), "0.16.0-preview.2", &stage).is_err());
        assert!(!stage.exists());
        stage_archive(&package(false, false), "0.16.0-preview.2", &stage).unwrap();
        assert_eq!(std::fs::read(stage.join("agent-core.exe")).unwrap(), b"exe");
        assert!(stage_archive(&package(false, false), "0.16.0-preview.2", &stage).is_err());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn failed_health_check_restores_all_program_files() {
        let root = std::env::temp_dir().join(format!("agent-update-test-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        let install = root.join("安装目录");
        fs::create_dir(&install).unwrap();
        for name in PROGRAM_FILES {
            fs::write(install.join(name), b"old").unwrap();
        }
        fs::write(
            install.join("application.json"),
            serde_json::to_vec(&serde_json::json!({
                "application_id": APPLICATION_ID, "version": "0.16.0-preview.1"
            }))
            .unwrap(),
        )
        .unwrap();
        let transaction = install
            .join(".agent-program-updates")
            .join(uuid::Uuid::new_v4().to_string());
        fs::create_dir_all(&transaction).unwrap();
        let stage = transaction.join("candidate");
        let manifest = stage_archive(&package(false, false), "0.16.0-preview.2", &stage).unwrap();
        let mut checked = false;
        assert!(publish_program(&install, &transaction, &manifest, || {
            checked = true;
            for name in PROGRAM_FILES {
                assert_eq!(fs::read(install.join(name)).unwrap(), b"exe");
            }
            Err("health_failed".into())
        })
        .is_err());
        assert!(checked);
        for name in PROGRAM_FILES {
            assert_eq!(fs::read(install.join(name)).unwrap(), b"old");
        }
        let identity: serde_json::Value =
            serde_json::from_slice(&fs::read(install.join("application.json")).unwrap()).unwrap();
        assert_eq!(identity["version"], "0.16.0-preview.1");
        fs::remove_dir_all(root).unwrap();
    }
}
