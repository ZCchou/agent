//! Standalone signed updater. No model or runtime downloads occur here.
use crate::{program_update::*, program_update_data::*, program_update_plan::*};
use std::{
    fs,
    path::Path,
    time::{Duration, Instant},
};

#[cfg(windows)]
fn exclusive_worker(transaction: &Path) -> Result<fs::File, String> {
    use std::os::windows::fs::OpenOptionsExt;
    fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .share_mode(0)
        .open(transaction.join("worker.lock"))
        .map_err(|_| "app_update_busy".into())
}

fn clear_pending(install: &Path, plan: &Handoff) -> Result<(), String> {
    let marker = install.join(".agent-update-pending.json");
    let value = read_json(&marker, 64 * 1024)?;
    if value["application_id"] != APPLICATION_ID || value["nonce"] != plan.nonce {
        return Err("app_update_handoff_invalid".into());
    }
    fs::remove_file(marker).map_err(|_| "app_update_install_failed".into())
}

#[cfg(windows)]
fn restart(install: &Path) -> Result<(), String> {
    use std::os::windows::process::CommandExt;
    std::process::Command::new(install.join("agent-desktop.exe"))
        .creation_flags(0x08000000)
        .spawn()
        .map(|_| ())
        .map_err(|_| "app_update_process_failed".into())
}

#[cfg(windows)]
fn probe_host(
    install: &Path,
    transaction: &Path,
    plan: &Handoff,
    manifest: &ProgramManifest,
) -> Result<(), String> {
    let contract = serde_json::to_value(
        manifest
            .core_contract
            .as_ref()
            .ok_or("app_update_feed_invalid")?,
    )
    .map_err(|_| "app_update_feed_invalid")?;
    let directory = transaction.to_str().ok_or("app_update_handoff_invalid")?;
    let mut child = crate::update_process::ManagedHost::spawn(
        &install.join("agent-desktop.exe"),
        "",
        &[
            ("AGENT_UPDATE_HEALTH_TRANSACTION", directory),
            ("AGENT_UPDATE_HEALTH_NONCE", plan.nonce.as_str()),
        ],
    )?;
    let deadline = Instant::now() + Duration::from_secs(60);
    let outcome = loop {
        let health = transaction.join("health.json");
        if health.exists() {
            break read_json(&health, 64 * 1024)
                .and_then(|value| validate_health(&value, plan, &contract));
        }
        match child.has_exited() {
            Ok(false) if Instant::now() < deadline => {}
            _ => break Err("app_update_health_failed".into()),
        }
        std::thread::sleep(Duration::from_millis(100));
    };
    // Stop the probe and its Core before commit or rollback. The final Host is
    // started only after the program journal has committed successfully.
    child
        .terminate_tree()
        .map_err(|_| "app_update_process_tree_unavailable")?;
    if outcome.is_err() {
        restore_profile(transaction).map_err(|_| "app_update_data_restore_failed")?;
    }
    outcome
}

#[cfg(windows)]
pub fn run(transaction: &Path) -> Result<(), String> {
    let transaction = transaction
        .canonicalize()
        .map_err(|_| "app_update_handoff_invalid")?;
    let _worker = exclusive_worker(&transaction)?;
    let install = transaction
        .parent()
        .and_then(Path::parent)
        .ok_or("app_update_handoff_invalid")?;
    let plan = validate_handoff(
        &read_json(&transaction.join("handoff.json"), 64 * 1024)?,
        install,
        &transaction,
    )?;
    let current = std::env::current_exe()
        .map_err(|_| "app_update_handoff_invalid")?
        .canonicalize()
        .map_err(|_| "app_update_handoff_invalid")?;
    if current != transaction.join("runner/agent-updater.exe") {
        return Err("app_update_handoff_invalid".into());
    }
    let parent =
        crate::update_process::Parent::open(plan.parent_pid, &install.join("agent-desktop.exe"))?;
    let owner = read_json(&install.join("application.json"), 1024 * 1024)?;
    if owner["application_id"] != APPLICATION_ID {
        return Err("app_update_feed_invalid".into());
    }
    let archive = transaction.join("package.zip");
    if file_identity(&archive)?.bytes > 512 * 1024 * 1024 {
        return Err("app_update_feed_invalid".into());
    }
    let signature = transaction.join("package.zip.sig");
    if file_identity(&signature)?.bytes > 16 * 1024 {
        return Err("app_update_signature_invalid".into());
    }
    let bytes = fs::read(archive).map_err(|_| "app_update_install_failed")?;
    verify_signed_bytes(
        &bytes,
        &fs::read(signature).map_err(|_| "app_update_signature_invalid")?,
        include_str!("../updater-public-key.txt"),
    )?;
    let manifest = stage_archive(&bytes, &plan.version, &transaction.join("candidate"))?;
    validate_upgrade(
        install,
        owner["version"].as_str().ok_or("app_update_feed_invalid")?,
        &manifest,
    )?;
    if manifest.core_contract.is_none() {
        return Err("app_update_feed_invalid".into());
    }
    write_json(
        &transaction.join("ready.json"),
        &serde_json::json!({"nonce":plan.nonce,"status":"ready"}),
    )?;
    parent.wait()?;
    if let Err(error) = backup_profile(&plan.data_root, &transaction) {
        clear_pending(install, &plan)?;
        restart(install)?;
        return Err(error);
    }
    let result = publish_program(install, &transaction, &manifest, || {
        probe_host(install, &transaction, &plan, &manifest)
    });
    if let Err(error) = result {
        if transaction.join("rolled-back.json").exists() {
            restore_profile(&transaction)?;
            clear_pending(install, &plan)?;
            restart(install)?;
        }
        return Err(error);
    }
    clear_pending(install, &plan)?;
    restart(install)?;
    write_json(
        &transaction.join("result.json"),
        &serde_json::json!({"status":"completed","version":plan.version}),
    )?;
    Ok(())
}

#[cfg(windows)]
pub fn recover(transaction: &Path) -> Result<(), String> {
    let transaction = transaction
        .canonicalize()
        .map_err(|_| "app_update_handoff_invalid")?;
    let _worker = exclusive_worker(&transaction)?;
    let install = transaction
        .parent()
        .and_then(Path::parent)
        .ok_or("app_update_handoff_invalid")?;
    let plan = validate_handoff(
        &read_json(&transaction.join("handoff.json"), 64 * 1024)?,
        install,
        &transaction,
    )?;
    let current = std::env::current_exe()
        .map_err(|_| "app_update_handoff_invalid")?
        .canonicalize()
        .map_err(|_| "app_update_handoff_invalid")?;
    if current != transaction.join("runner/agent-updater.exe") {
        return Err("app_update_handoff_invalid".into());
    }
    let parent =
        crate::update_process::Parent::open(plan.parent_pid, &install.join("agent-desktop.exe"))?;
    // Backups and the pending program journal are verified again by restore_*.
    let ready = transaction.join("recovery-ready.json");
    if ready.exists() {
        fs::remove_file(&ready).map_err(|_| "app_update_install_failed")?;
    }
    write_json(
        &ready,
        &serde_json::json!({"nonce":plan.nonce,"parent_pid":plan.parent_pid,"status":"ready"}),
    )?;
    parent.wait()?;
    if !transaction.join("completed.json").exists() {
        if transaction.join("data-backup.json").exists() {
            restore_profile(&transaction)?;
        } else if transaction.join("pending.json").exists() {
            return Err("app_update_data_restore_failed".into());
        }
        if transaction.join("pending.json").exists() {
            restore_program(install, &transaction)?;
        }
    }
    clear_pending(install, &plan)?;
    restart(install)
}
