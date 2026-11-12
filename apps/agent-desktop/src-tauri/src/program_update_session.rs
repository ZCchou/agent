//! Host-side handoff and startup recovery for signed program updates.
use crate::{program_update::*, program_update_plan::*};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Child,
    sync::Mutex,
    time::{Duration, Instant},
};
use tauri::{AppHandle, Manager};

pub fn is_health_probe() -> bool {
    std::env::var_os("AGENT_UPDATE_HEALTH_TRANSACTION").is_some()
}

fn install_root() -> Result<PathBuf, String> {
    std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(Path::to_path_buf))
        .and_then(|path| path.canonicalize().ok())
        .ok_or("app_update_install_failed".into())
}

fn launch(transaction: &Path, recovery: bool) -> Result<Child, String> {
    let mut command = std::process::Command::new(transaction.join("runner/agent-updater.exe"));
    command
        .arg(transaction)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    if recovery {
        command.arg("--recover");
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    command
        .spawn()
        .map_err(|_| "app_update_handoff_failed".into())
}

fn wait_ready(
    child: &mut Child,
    transaction: &Path,
    nonce: &str,
    recovery: bool,
) -> Result<(), String> {
    let ready = transaction.join(if recovery {
        "recovery-ready.json"
    } else {
        "ready.json"
    });
    let result = transaction.join(if recovery {
        "recovery-result.json"
    } else {
        "result.json"
    });
    let deadline = Instant::now() + Duration::from_secs(60);
    loop {
        if ready.exists() {
            let value = read_json(&ready, 64 * 1024)?;
            if value["nonce"] == nonce
                && value["status"] == "ready"
                && (!recovery || value["parent_pid"].as_u64() == Some(std::process::id() as u64))
            {
                return Ok(());
            }
        }
        if result.exists() {
            return Err(read_json(&result, 64 * 1024)?["error_code"]
                .as_str()
                .unwrap_or("app_update_handoff_failed")
                .into());
        }
        if child
            .try_wait()
            .map_err(|_| "app_update_handoff_failed")?
            .is_some()
            || Instant::now() >= deadline
        {
            return Err("app_update_handoff_failed".into());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

fn remove_pending(install: &Path, nonce: &str) -> Result<(), String> {
    let marker = install.join(".agent-update-pending.json");
    if marker.exists() {
        let value = read_json(&marker, 64 * 1024)?;
        if value["application_id"] != APPLICATION_ID || value["nonce"] != nonce {
            return Err("app_update_handoff_invalid".into());
        }
        fs::remove_file(marker).map_err(|_| "app_update_install_failed")?;
    }
    Ok(())
}

pub fn prepare_and_handoff(
    data_root: &Path,
    bytes: &[u8],
    signature: &str,
    version: &str,
    id: &str,
) -> Result<(), String> {
    verify_signed_bytes(
        bytes,
        signature.as_bytes(),
        include_str!("../updater-public-key.txt"),
    )?;
    if bytes.len() > 512 * 1024 * 1024 || uuid::Uuid::parse_str(id).is_err() {
        return Err("app_update_feed_invalid".into());
    }
    let install = install_root()?;
    let owner = read_json(&install.join("application.json"), 1024 * 1024)?;
    if owner["application_id"] != APPLICATION_ID || owner["version"] != env!("CARGO_PKG_VERSION") {
        return Err("app_update_offline_install_required".into());
    }
    if install.join(".agent-update-pending.json").exists() {
        return Err("app_update_busy".into());
    }
    let expected: FileIdentity =
        serde_json::from_value(owner["files"]["agent-updater.exe"].clone())
            .map_err(|_| "app_update_offline_install_required")?;
    let root = install.join(".agent-program-updates");
    if root.exists() {
        if read_json(&root.join("owner.json"), 64 * 1024)?["application_id"] != APPLICATION_ID {
            return Err("app_update_handoff_invalid".into());
        }
    } else {
        fs::create_dir(&root).map_err(|_| "app_update_install_failed")?;
        write_json(
            &root.join("owner.json"),
            &serde_json::json!({"application_id":APPLICATION_ID}),
        )?;
    }
    let transaction = root.join(id);
    fs::create_dir(&transaction).map_err(|_| "app_update_install_failed")?;
    fs::create_dir(transaction.join("runner")).map_err(|_| "app_update_install_failed")?;
    fs::copy(
        install.join("agent-updater.exe"),
        transaction.join("runner/agent-updater.exe"),
    )
    .map_err(|_| "app_update_handoff_failed")?;
    let actual = file_identity(&transaction.join("runner/agent-updater.exe"))?;
    if actual.sha256 != expected.sha256 || actual.bytes != expected.bytes {
        return Err("app_update_signature_invalid".into());
    }
    let plan = serde_json::json!({"schema_version":1,"application_id":APPLICATION_ID,"version":version,"nonce":id,
        "parent_pid":std::process::id(),"data_root":data_root});
    validate_handoff(&plan, &install, &transaction)?;
    write_json(&transaction.join("handoff.json"), &plan)?;
    fs::write(transaction.join("package.zip"), bytes).map_err(|_| "app_update_install_failed")?;
    fs::write(transaction.join("package.zip.sig"), signature)
        .map_err(|_| "app_update_install_failed")?;
    replace_json(
        &root.join("latest.json"),
        &serde_json::json!({"application_id":APPLICATION_ID,"nonce":id}),
    )?;
    write_json(
        &install.join(".agent-update-pending.json"),
        &serde_json::json!({"application_id":APPLICATION_ID,"nonce":id}),
    )?;
    let result = (|| {
        let mut child = launch(&transaction, false)?;
        if let Err(error) = wait_ready(&mut child, &transaction, id, false) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
        // Child intentionally remains alive after the Host exits.
        Ok(())
    })();
    if result.is_err() {
        remove_pending(&install, id)?;
    }
    result
}

pub fn startup(app: &AppHandle) -> Result<bool, String> {
    let install = install_root()?;
    if let Some(directory) = std::env::var_os("AGENT_UPDATE_HEALTH_TRANSACTION") {
        let transaction = PathBuf::from(directory);
        let plan = validate_handoff(
            &read_json(&transaction.join("handoff.json"), 64 * 1024)?,
            &install,
            &transaction,
        )?;
        if std::env::var("AGENT_UPDATE_HEALTH_NONCE").ok().as_deref() != Some(plan.nonce.as_str()) {
            return Err("app_update_handoff_invalid".into());
        }
        if app
            .path()
            .app_data_dir()
            .ok()
            .and_then(|root| root.canonicalize().ok())
            != plan.data_root.canonicalize().ok()
        {
            return Err("app_update_data_invalid".into());
        }
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.hide();
        }
        return Ok(false);
    }
    let marker = install.join(".agent-update-pending.json");
    if !marker.exists() {
        return Ok(false);
    }
    let value = read_json(&marker, 64 * 1024)?;
    if value["application_id"] != APPLICATION_ID {
        return Err("app_update_handoff_invalid".into());
    }
    let nonce = value["nonce"]
        .as_str()
        .ok_or("app_update_handoff_invalid")?;
    if uuid::Uuid::parse_str(nonce).is_err() {
        return Err("app_update_handoff_invalid".into());
    }
    let transaction = install.join(".agent-program-updates").join(nonce);
    let mut plan = read_json(&transaction.join("handoff.json"), 64 * 1024)?;
    validate_handoff(&plan, &install, &transaction)?;
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        if transaction.join("worker.lock").exists()
            && fs::OpenOptions::new()
                .read(true)
                .write(true)
                .share_mode(0)
                .open(transaction.join("worker.lock"))
                .is_err()
        {
            return Ok(true);
        }
    }
    // The original worker exited. Recover under an exact new parent handle.
    plan["parent_pid"] = serde_json::json!(std::process::id());
    replace_json(&transaction.join("handoff.json"), &plan)?;
    let recovery_result = transaction.join("recovery-result.json");
    if recovery_result.exists() {
        fs::remove_file(recovery_result).map_err(|_| "app_update_install_failed")?;
    }
    let mut child = launch(&transaction, true)?;
    if let Err(error) = wait_ready(&mut child, &transaction, nonce, true) {
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }
    Ok(true)
}

pub async fn acknowledge_health(app: AppHandle) -> Result<(), String> {
    let Some(directory) = std::env::var_os("AGENT_UPDATE_HEALTH_TRANSACTION") else {
        return Ok(());
    };
    let install = install_root()?;
    let transaction = PathBuf::from(directory);
    let plan = validate_handoff(
        &read_json(&transaction.join("handoff.json"), 64 * 1024)?,
        &install,
        &transaction,
    )?;
    let owner = read_json(&install.join("application.json"), 1024 * 1024)?;
    let manager = app.state::<Mutex<crate::core_process_manager::CoreProcessManager>>();
    let core =
        crate::core_bridge::request_core(&app, &manager, "ping", serde_json::json!({})).await?;
    let response = serde_json::json!({"application_id":APPLICATION_ID,"version":env!("CARGO_PKG_VERSION"),"nonce":plan.nonce,"core":core});
    validate_health(&response, &plan, &owner["core_contract"])?;
    let path = transaction.join("health.json");
    if path.exists() {
        return validate_health(
            &read_json(&path, 64 * 1024)?,
            &plan,
            &owner["core_contract"],
        );
    }
    write_json(&path, &response)
}

pub fn previous_update_error() -> Option<String> {
    let install = install_root().ok()?;
    let latest = read_json(
        &install.join(".agent-program-updates/latest.json"),
        64 * 1024,
    )
    .ok()?;
    if latest["application_id"] != APPLICATION_ID {
        return None;
    }
    let nonce = latest["nonce"].as_str()?;
    if uuid::Uuid::parse_str(nonce).ok()?.to_string() != nonce {
        return None;
    }
    let transaction = install.join(".agent-program-updates").join(nonce);
    if transaction.join("rolled-back.json").is_file() {
        return Some("app_update_rolled_back".into());
    }
    let result = read_json(&transaction.join("result.json"), 64 * 1024).ok()?;
    if result["status"] == "failed" {
        return result["error_code"].as_str().map(str::to_owned);
    }
    None
}
