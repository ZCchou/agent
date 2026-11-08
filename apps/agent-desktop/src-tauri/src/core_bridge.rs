use std::{
    sync::Mutex,
    time::{Duration, Instant},
};

use serde::Serialize;
use serde_json::{json, Value};
use tauri::{AppHandle, Manager, State};

use crate::{
    core_process_manager::{CoreProcessManager, RequestOwner},
    logging::{events as log_events, AppLogger, LogLevel},
};

pub(crate) const PROTOCOL_VERSION: u64 = 18;
const EXPECTED_CORE_VERSION: &str = env!("CARGO_PKG_VERSION");
const EXPECTED_SCHEMA_VERSION: u64 = 25;
const EXPECTED_BUILD_ID: &str = "schema25-local-runtime-v1";
const CORE_REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
// Automatic start preparation may make two bounded 30-second intent calls
// before returning a run. Control RPCs must not inherit that longer wait.
const MODEL_PREPARATION_TIMEOUT: Duration = Duration::from_secs(90);

fn default_request_timeout(message_type: &str) -> Duration {
    match message_type {
        "model.run.start" | "model.run.retry" => MODEL_PREPARATION_TIMEOUT,
        _ => CORE_REQUEST_TIMEOUT,
    }
}

fn needs_capability_observation(message_type: &str) -> bool {
    matches!(
        message_type,
        "model.run.start" | "model.run.retry" | "model.run.context.continue"
    )
}

async fn receive_with_timeout(
    receiver: &mut tauri::async_runtime::Receiver<Value>,
    duration: Duration,
) -> Result<Option<Value>, tokio::time::error::Elapsed> {
    tokio::time::timeout(duration, receiver.recv()).await
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct CoreStatus {
    pub running: bool,
    pub healthy: bool,
}

#[tauri::command]
pub fn start_agent_core(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
) -> Result<CoreStatus, String> {
    let mut manager = manager
        .lock()
        .map_err(|_| "Failed to lock CoreProcessManager".to_string())?;
    manager.start(&app)?;
    Ok(CoreStatus {
        running: manager.is_running(),
        healthy: manager.is_healthy(),
    })
}

#[tauri::command]
pub fn agent_core_status(
    manager: State<'_, Mutex<CoreProcessManager>>,
) -> Result<CoreStatus, String> {
    let manager = manager
        .lock()
        .map_err(|_| "Failed to lock CoreProcessManager".to_string())?;
    Ok(CoreStatus {
        running: manager.is_running(),
        healthy: manager.is_healthy(),
    })
}

#[tauri::command]
pub fn shutdown_agent_core(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
) -> Result<CoreStatus, String> {
    app.state::<AppLogger>().host(
        LogLevel::Info,
        log_events::CORE_STOP_REQUESTED,
        "Agent Core stop requested",
        json!({}),
    );
    let mut manager = manager
        .lock()
        .map_err(|_| "Failed to lock CoreProcessManager".to_string())?;
    app.state::<crate::file_search::FileSearchService>()
        .invalidate_all();
    if let Err(error) = manager.shutdown() {
        app.state::<AppLogger>().host(
            LogLevel::Error,
            log_events::CORE_STOP_FAILED,
            "Failed to stop Agent Core",
            json!({"error_type": "core_shutdown_failed"}),
        );
        return Err(error);
    }
    let status = CoreStatus {
        running: manager.is_running(),
        healthy: manager.is_healthy(),
    };
    app.state::<AppLogger>().host(
        LogLevel::Info,
        log_events::CORE_STOPPED,
        "Agent Core shutdown command completed",
        json!({"running": status.running}),
    );
    Ok(status)
}

fn generate_request_id() -> String {
    format!("req-{}", uuid::Uuid::new_v4())
}

fn validate_response(
    response: &Value,
    expected_id: &str,
    expected_type: &str,
) -> Result<Value, String> {
    if response.get("v").and_then(Value::as_u64) != Some(PROTOCOL_VERSION) {
        return Err("Agent Core response protocol version mismatch".to_string());
    }
    if response.get("id").and_then(Value::as_str) != Some(expected_id) {
        return Err("Agent Core response id mismatch".to_string());
    }
    if response.get("type").and_then(Value::as_str) != Some(expected_type) {
        return Err("Agent Core response type mismatch".to_string());
    }
    match (response.get("result"), response.get("error")) {
        (Some(result), None) => Ok(result.clone()),
        (None, Some(error)) => {
            let code = error
                .get("code")
                .and_then(Value::as_str)
                .unwrap_or("core_error");
            let message = error
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("Core request failed.");
            Err(format!("{code}: {message}"))
        }
        _ => Err("Agent Core response must contain exactly one of result or error".to_string()),
    }
}

pub async fn request_core(
    app: &AppHandle,
    manager: &State<'_, Mutex<CoreProcessManager>>,
    message_type: &str,
    params: Value,
) -> Result<Value, String> {
    if needs_capability_observation(message_type) {
        // A missing observation protocol must not prevent an ordinary answer.
        let _ = request_core_with_timeout(
            app,
            manager,
            "runtime.capabilities.configure",
            json!({"host_reporting":true}),
            CORE_REQUEST_TIMEOUT,
        )
        .await;
    }
    request_core_with_timeout(
        app,
        manager,
        message_type,
        params,
        default_request_timeout(message_type),
    )
    .await
}

pub async fn request_core_with_timeout(
    app: &AppHandle,
    manager: &State<'_, Mutex<CoreProcessManager>>,
    message_type: &str,
    params: Value,
    timeout: Duration,
) -> Result<Value, String> {
    request_core_with_dispatch(
        app,
        manager,
        message_type,
        params,
        timeout,
        |manager, id, message| {
            let result = manager.send_request(id, message);
            if result.is_err() {
                manager.set_healthy(false);
            }
            result
        },
    )
    .await
}

pub(crate) async fn request_core_with_dispatch<F>(
    app: &AppHandle,
    manager: &State<'_, Mutex<CoreProcessManager>>,
    message_type: &str,
    params: Value,
    timeout: Duration,
    dispatch: F,
) -> Result<Value, String>
where
    F: FnOnce(
            &mut CoreProcessManager,
            &str,
            &str,
        ) -> Result<tauri::async_runtime::Receiver<Value>, String>
        + Send,
{
    crate::offline::validate_dispatch(message_type, &params)?;
    if matches!(
        message_type,
        "model.run.start"
            | "provider.list_models"
            | "model.tool_capability.probe"
            | "context.compaction.resume"
    ) {
        use tauri::Manager;
        let provider_id = params
            .get("provider_profile_id")
            .and_then(Value::as_str)
            .or_else(|| {
                params
                    .get("target")
                    .and_then(|target| target.get("provider_profile_id"))
                    .and_then(Value::as_str)
            });
        if let (Some(provider_id), Some(settings)) = (
            provider_id,
            app.try_state::<crate::settings::SettingsManager>(),
        ) {
            if !provider_id.starts_with("managed:") {
                let configured = settings.get_settings().map_err(|error| error.to_string())?;
                let profile = configured
                    .models
                    .providers
                    .iter()
                    .find(|profile| profile.id == provider_id)
                    .ok_or("provider_not_found")?;
                crate::offline::validate_profile(profile)?;
            }
        }
    }

    let started_at = Instant::now();
    let request_id = generate_request_id();
    let request =
        json!({"v": PROTOCOL_VERSION, "id": request_id, "type": message_type, "params": params});
    let message = serde_json::to_string(&request)
        .map_err(|_| "Failed to serialize Core request".to_string())?;

    app.state::<AppLogger>().ipc(
        LogLevel::Debug,
        log_events::IPC_REQUEST_STARTED,
        "IPC request started",
        json!({"request_id": request_id, "message_type": message_type,
            "protocol_version": PROTOCOL_VERSION}),
    );

    let (mut receiver, owner) = {
        let mut manager = manager
            .lock()
            .map_err(|_| "Failed to lock CoreProcessManager".to_string())?;
        // A guard rejection is not evidence that Core itself is unhealthy.
        let receiver = dispatch(&mut manager, &request_id, &message)?;
        (receiver, manager.request_owner())
    };

    let received = receive_with_timeout(&mut receiver, timeout).await;
    if received.is_err() {
        app.state::<AppLogger>().ipc(
            LogLevel::Error,
            log_events::IPC_REQUEST_FAILED,
            "IPC request timed out",
            json!({"request_id": request_id, "message_type": message_type,
                    "duration_ms": started_at.elapsed().as_secs_f64() * 1000.0,
                    "error_type": "request_timeout"}),
        );
    }
    let result = finish_core_response(
        manager,
        &owner,
        &request_id,
        message_type,
        timeout,
        received,
    )?;
    app.state::<AppLogger>().ipc(
        LogLevel::Info,
        log_events::IPC_REQUEST_COMPLETED,
        "IPC request completed",
        json!({"request_id": request_id, "message_type": message_type,
            "duration_ms": started_at.elapsed().as_secs_f64() * 1000.0, "success": true}),
    );
    Ok(result)
}

// Complete the actual waiter result without requiring UI state in lifecycle tests.
fn finish_core_response(
    manager: &Mutex<CoreProcessManager>,
    owner: &RequestOwner,
    request_id: &str,
    message_type: &str,
    timeout: Duration,
    received: Result<Option<Value>, tokio::time::error::Elapsed>,
) -> Result<Value, String> {
    // Keep the ownership check and every cleanup/health write in one manager lock.
    // A stale completion still returns its own result, but cannot touch the replacement.
    let manager = manager
        .lock()
        .ok()
        .filter(|manager| manager.owns_request(owner));
    let response = match received {
        Ok(Some(response)) => response,
        Ok(None) => {
            if let Some(manager) = manager.as_ref() {
                manager.cancel_request(request_id);
                manager.set_healthy(false);
            }
            return Err("Agent Core response channel closed".to_string());
        }
        Err(_) => {
            if let Some(manager) = manager.as_ref() {
                manager.cancel_request(request_id);
                if message_type == "ping" {
                    manager.set_healthy(false);
                }
            }
            return Err(format!(
                "Agent Core request timed out after {} seconds",
                timeout.as_secs()
            ));
        }
    };

    let result = validate_response(&response, request_id, message_type).inspect_err(|_error| {
        if let Some(manager) = manager.as_ref() {
            if message_type == "ping" {
                manager.set_healthy(false);
            }
        }
    })?;

    if message_type == "ping" {
        let compatible = result.get("core_version").and_then(Value::as_str)
            == Some(EXPECTED_CORE_VERSION)
            && result.get("protocol_version").and_then(Value::as_u64) == Some(PROTOCOL_VERSION)
            && result.get("schema_version").and_then(Value::as_u64)
                == Some(EXPECTED_SCHEMA_VERSION)
            && result.get("build_id").and_then(Value::as_str) == Some(EXPECTED_BUILD_ID);
        if !compatible {
            if let Some(manager) = manager.as_ref() {
                manager.set_healthy(false);
            }
            return Err(format!(
                "Agent Core sidecar is incompatible; rebuild it with tools\\build-python-sidecar.bat (expected Core {EXPECTED_CORE_VERSION}, schema {EXPECTED_SCHEMA_VERSION})"
            ));
        }
        if let Some(manager) = manager.as_ref() {
            manager.set_healthy(true);
        }
    }
    Ok(result)
}

#[tauri::command]
pub async fn ping_agent_core(
    app: AppHandle,
    manager: State<'_, Mutex<CoreProcessManager>>,
) -> Result<Value, String> {
    request_core(&app, &manager, "ping", json!({})).await
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::{receive_with_timeout, validate_response};
    use serde_json::json;

    #[test]
    fn generation_entries_initialize_observation_after_a_core_restart() {
        // waiting_context survives restart; continuing it can be the first
        // generation request sent to the fresh Core, just like start/retry.
        for entry in [
            "model.run.start",
            "model.run.retry",
            "model.run.context.continue",
        ] {
            assert!(super::needs_capability_observation(entry), "{entry}");
        }
        for control in [
            "model.run.cancel",
            "model.run.get",
            "runtime.capabilities.resolve",
        ] {
            assert!(!super::needs_capability_observation(control), "{control}");
        }
    }

    #[tokio::test]
    async fn model_preparation_response_survives_ordinary_ten_second_deadline() {
        async fn delayed_response(message_type: &'static str) {
            let manager =
                std::sync::Mutex::new(crate::core_process_manager::CoreProcessManager::new());
            manager.lock().unwrap().reset_process_state();
            manager.lock().unwrap().set_healthy(true);
            let owner = manager.lock().unwrap().request_owner();
            let (sender, mut receiver) = tauri::async_runtime::channel(1);
            tokio::spawn(async move {
                tokio::time::sleep(Duration::from_secs(11)).await;
                let _ = sender
                    .send(json!({
                        "v": super::PROTOCOL_VERSION, "id": "prepared", "type": message_type,
                        "result": {"run": {"id": "prepared-run", "status": "queued"}}
                    }))
                    .await;
            });
            let timeout = super::default_request_timeout(message_type);
            let received = receive_with_timeout(&mut receiver, timeout).await;
            let result = super::finish_core_response(
                &manager,
                &owner,
                "prepared",
                message_type,
                timeout,
                received,
            )
            .unwrap_or_else(|error| panic!("{message_type}: {error}"));
            assert_eq!(result["run"]["id"], "prepared-run");
            assert!(manager.lock().unwrap().is_healthy());
        }
        tokio::join!(
            delayed_response("model.run.start"),
            delayed_response("model.run.retry")
        );
    }

    #[test]
    fn preparation_timeout_is_bounded_and_does_not_lengthen_control_requests() {
        for message_type in ["model.run.start", "model.run.retry"] {
            assert_eq!(
                super::default_request_timeout(message_type),
                Duration::from_secs(90)
            );
        }
        for message_type in [
            "ping",
            "model.run.get",
            "model.run.cancel",
            "model.run.tool.resolve",
            "conversation.create",
        ] {
            assert_eq!(
                super::default_request_timeout(message_type),
                Duration::from_secs(10)
            );
        }
    }

    #[tokio::test]
    async fn old_closed_waiter_cannot_mark_replacement_unhealthy() {
        let manager = std::sync::Mutex::new(crate::core_process_manager::CoreProcessManager::new());
        manager.lock().unwrap().reset_process_state();
        let owner = manager.lock().unwrap().request_owner();
        let (sender, mut receiver) = tauri::async_runtime::channel(1);
        drop(sender);
        let received = receive_with_timeout(&mut receiver, Duration::from_secs(1)).await;
        {
            let mut current = manager.lock().unwrap();
            current.reset_process_state();
            current.set_healthy(true);
        }
        assert!(super::finish_core_response(
            &manager,
            &owner,
            "old",
            "ping",
            Duration::from_secs(1),
            received
        )
        .is_err());
        assert!(manager.lock().unwrap().is_healthy());
    }

    #[tokio::test]
    async fn old_successful_ping_cannot_mark_replacement_healthy() {
        let manager = std::sync::Mutex::new(crate::core_process_manager::CoreProcessManager::new());
        manager.lock().unwrap().reset_process_state();
        let owner = manager.lock().unwrap().request_owner();
        let (sender, mut receiver) = tauri::async_runtime::channel(1);
        sender.send(json!({"v": super::PROTOCOL_VERSION, "id": "old", "type": "ping", "result": {
            "core_version": super::EXPECTED_CORE_VERSION, "protocol_version": super::PROTOCOL_VERSION,
            "schema_version": super::EXPECTED_SCHEMA_VERSION, "build_id": super::EXPECTED_BUILD_ID
        }})).await.unwrap();
        let received = receive_with_timeout(&mut receiver, Duration::from_secs(1)).await;
        manager.lock().unwrap().reset_process_state();
        assert!(super::finish_core_response(
            &manager,
            &owner,
            "old",
            "ping",
            Duration::from_secs(1),
            received
        )
        .is_ok());
        assert!(!manager.lock().unwrap().is_healthy());
    }

    #[tokio::test]
    async fn stale_ping_timeout_and_invalid_responses_preserve_replacement_health() {
        let (_sender, mut receiver) = tauri::async_runtime::channel(1);
        let timeout = Duration::from_millis(1);
        let received_timeout = receive_with_timeout(&mut receiver, timeout).await;
        assert!(received_timeout.is_err());
        for received in [
            received_timeout,
            Ok(Some(
                json!({"v": 1, "id": "old", "type": "ping", "result": {}}),
            )),
            Ok(Some(
                json!({"v": super::PROTOCOL_VERSION, "id": "old", "type": "ping", "result": {}}),
            )),
            Ok(Some(
                json!({"v": super::PROTOCOL_VERSION, "id": "old", "type": "ping", "error": {"code": "failure"}}),
            )),
        ] {
            let manager =
                std::sync::Mutex::new(crate::core_process_manager::CoreProcessManager::new());
            manager.lock().unwrap().reset_process_state();
            let owner = manager.lock().unwrap().request_owner();
            {
                let mut current = manager.lock().unwrap();
                current.reset_process_state();
                current.set_healthy(true);
            }
            assert!(super::finish_core_response(
                &manager, &owner, "old", "ping", timeout, received
            )
            .is_err());
            assert!(manager.lock().unwrap().is_healthy());
        }
    }

    #[test]
    fn current_ping_completion_still_updates_health() {
        let manager = std::sync::Mutex::new(crate::core_process_manager::CoreProcessManager::new());
        manager.lock().unwrap().reset_process_state();
        let owner = manager.lock().unwrap().request_owner();
        let response = json!({"v": 18, "id": "current", "type": "ping", "result": {
            "core_version": super::EXPECTED_CORE_VERSION, "protocol_version": 18,
            "schema_version": 25, "build_id": "schema25-local-runtime-v1"
        }});
        assert!(super::finish_core_response(
            &manager,
            &owner,
            "current",
            "ping",
            Duration::from_secs(1),
            Ok(Some(response))
        )
        .is_ok());
        assert!(manager.lock().unwrap().is_healthy());
        assert!(super::finish_core_response(
            &manager,
            &owner,
            "current",
            "ping",
            Duration::from_secs(1),
            Ok(None)
        )
        .is_err());
        assert!(!manager.lock().unwrap().is_healthy());
    }

    #[test]
    fn legacy_sidecar_ping_is_rejected_and_marks_current_manager_unhealthy() {
        let manager = std::sync::Mutex::new(crate::core_process_manager::CoreProcessManager::new());
        manager.lock().unwrap().reset_process_state();
        let owner = manager.lock().unwrap().request_owner();
        manager.lock().unwrap().set_healthy(true);
        let legacy_response = json!({"v": 15, "id": "current", "type": "ping", "result": {
            "core_version": "0.13.0", "protocol_version": 15,
            "schema_version": 20, "build_id": "schema20-global-instructions-contract-v1"
        }});
        assert!(super::finish_core_response(
            &manager,
            &owner,
            "current",
            "ping",
            Duration::from_secs(1),
            Ok(Some(legacy_response))
        )
        .is_err());
        assert!(!manager.lock().unwrap().is_healthy());
    }

    #[test]
    fn current_ipc_ping_rejects_each_incompatible_core_identity() {
        let current = json!({
            "core_version": super::EXPECTED_CORE_VERSION, "protocol_version": 18,
            "schema_version": 25, "build_id": "schema25-local-runtime-v1"
        });
        for (field, wrong_value) in [
            ("core_version", json!("0.13.0")),
            ("schema_version", json!(20)),
            (
                "build_id",
                json!("schema20-global-instructions-contract-v1"),
            ),
        ] {
            let manager =
                std::sync::Mutex::new(crate::core_process_manager::CoreProcessManager::new());
            manager.lock().unwrap().reset_process_state();
            let owner = manager.lock().unwrap().request_owner();
            manager.lock().unwrap().set_healthy(true);
            let mut identity = current.clone();
            identity[field] = wrong_value;
            let response = json!({"v": 18, "id": "current", "type": "ping", "result": identity});
            assert!(
                super::finish_core_response(
                    &manager,
                    &owner,
                    "current",
                    "ping",
                    Duration::from_secs(1),
                    Ok(Some(response))
                )
                .is_err(),
                "accepted wrong {field}"
            );
            assert!(
                !manager.lock().unwrap().is_healthy(),
                "wrong {field} retained health"
            );
        }
    }

    #[test]
    fn validates_success_envelope() {
        let response = json!({"v": 18, "id": "req-1", "type": "ping", "result": {"pid": 42}});
        assert_eq!(
            validate_response(&response, "req-1", "ping").unwrap()["pid"],
            42
        );
    }

    #[test]
    fn rejects_protocol_type_and_id_mismatches() {
        for response in [
            json!({"v": 1, "id": "req-1", "type": "ping", "result": {}}),
            json!({"v": 18, "id": "wrong", "type": "ping", "result": {}}),
            json!({"v": 18, "id": "req-1", "type": "other", "result": {}}),
        ] {
            assert!(validate_response(&response, "req-1", "ping").is_err());
        }
    }

    #[test]
    fn surfaces_structured_core_error() {
        let response = json!({"v": 18, "id": "req-1", "type": "ping",
            "error": {"code": "bad_request", "message": "Nope"}});
        assert!(validate_response(&response, "req-1", "ping")
            .unwrap_err()
            .contains("bad_request"));
    }

    #[tokio::test]
    async fn response_wait_has_a_real_timeout() {
        let (_sender, mut receiver) = tauri::async_runtime::channel(1);
        assert!(
            receive_with_timeout(&mut receiver, Duration::from_millis(1))
                .await
                .is_err()
        );
    }
}
