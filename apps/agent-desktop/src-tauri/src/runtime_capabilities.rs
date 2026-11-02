use crate::{
    agent_execution::{AgentExecutionManager, MAX_CHANGE_BYTES, MAX_READ_BYTES},
    change_set_planner::MAX_MUTATIONS,
    core_bridge::request_core,
    core_process_manager::CoreProcessManager,
    file_access::FileAccessPolicy,
    terminal_executor::{MAX_OUTPUT_BYTES, MAX_TIMEOUT_MS},
    work_tools::{ApprovalMode, WorkToolManager},
};
use serde_json::{json, Value};
use std::sync::Mutex;
use tauri::{AppHandle, Manager};

fn observe_managers(
    tools: &WorkToolManager,
    conversation: &str,
    project: Option<&str>,
    executor_available: bool,
    shell_available: bool,
) -> Value {
    let grant = project.and_then(|id| tools.project_grant(id));
    let access = project.and_then(|id| tools.authorized_access(id, conversation).ok());
    let workspace_available = access.as_ref().is_some_and(|root| {
        FileAccessPolicy
            .validate_relative(".")
            .ok()
            .and_then(|path| FileAccessPolicy.open_read(root, &path).ok())
            .is_some()
    });
    let permission = project.and_then(|id| tools.work_permission(conversation, id).ok());
    let mode = permission.as_ref().map(|p| p.mode);
    let reason = if project.is_none() {
        "not_project_work"
    } else if grant.as_ref().is_some_and(|grant| !grant.active) {
        "workspace_suspended"
    } else {
        "workspace_not_authorized"
    };
    let read_status = if workspace_available && executor_available {
        "available"
    } else {
        "unavailable"
    };
    let read_reason = if !workspace_available {
        Some(reason)
    } else if !executor_available {
        Some("executor_unavailable")
    } else {
        None
    };
    let action_status = if !workspace_available {
        "unavailable"
    } else if mode == Some(ApprovalMode::Open) {
        "available"
    } else {
        "requires_approval"
    };
    let mut report = json!({"version":1,
    "workspace":{"status":if workspace_available {"available"} else {"unavailable"},
                 "root_generation":grant.as_ref().map(|grant|grant.root_generation)},
    "approval_mode":mode,
    "environment":{"platform":std::env::consts::OS,"shell":if shell_available {"powershell"} else {"unknown"},
                   "interactive_stdin":false,"detached_processes":false},
    "tools":{
        "fs.list":{"status":read_status,"reason_code":read_reason},
        "fs.read":{"status":read_status,"reason_code":read_reason,"limits":{"max_read_bytes":MAX_READ_BYTES}},
        "fs.search":{"status":if workspace_available {"available"} else {"unavailable"},"reason_code":if workspace_available {None} else {Some(reason)}},
        "fs.propose_changes":{"status":if executor_available {action_status} else {"unavailable"},
            "reason_code":if !workspace_available {Some(reason)} else if !executor_available {Some("executor_unavailable")} else {None},
            "limits":{"max_change_bytes":MAX_CHANGE_BYTES,"max_mutations":MAX_MUTATIONS}},
        "terminal.run":{"status":if !shell_available {"unavailable"} else {action_status},
            "reason_code":if !workspace_available {Some(reason)} else if !shell_available {Some("powershell_unavailable")} else {None},
            "limits":{"max_timeout_ms":MAX_TIMEOUT_MS,"max_output_bytes":MAX_OUTPUT_BYTES}}
    }});
    if permission.is_none() {
        report["approval_mode"] = json!("unknown");
    }
    report
}

pub(crate) async fn handle(app: &AppHandle, event: &Value) -> Result<(), String> {
    let run_id = event
        .get("run_id")
        .and_then(Value::as_str)
        .ok_or("missing run scope")?;
    let request_id = event
        .pointer("/data/request_id")
        .and_then(Value::as_str)
        .ok_or("missing capability request")?;
    let manager = app.state::<Mutex<CoreProcessManager>>();
    let run = request_core(app, &manager, "model.run.get", json!({"run_id":run_id})).await?;
    let conversation = run
        .pointer("/run/conversation_id")
        .and_then(Value::as_str)
        .ok_or("missing conversation scope")?;
    if event
        .pointer("/data/conversation_id")
        .and_then(Value::as_str)
        != Some(conversation)
    {
        return Err("capability_scope_mismatch".into());
    }
    let scope = request_core(
        app,
        &manager,
        "conversation.get",
        json!({"conversation_id":conversation}),
    )
    .await?;
    let project = scope
        .pointer("/conversation/project_id")
        .and_then(Value::as_str);
    let executor_available = app
        .state::<Mutex<AgentExecutionManager>>()
        .lock()
        .map(|manager| manager.ensure_available().is_ok())
        .unwrap_or(false);
    #[cfg(windows)]
    let shell_available =
        crate::terminal_executor::powershell_path().is_ok_and(|path| path.is_file());
    #[cfg(not(windows))]
    let shell_available = false;
    let tools = app.state::<Mutex<WorkToolManager>>();
    let report = match tools.lock() {
        Ok(tools) => observe_managers(
            &tools,
            conversation,
            project,
            executor_available,
            shell_available,
        ),
        Err(_) => {
            json!({"version":1,"workspace":{"status":"unknown"},"approval_mode":"unknown","tools":{}})
        }
    };
    request_core(
        app,
        &manager,
        "runtime.capabilities.resolve",
        json!({"run_id":run_id,"request_id":request_id,"report":report}),
    )
    .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::work_tools::{ApprovalMode, WorkToolManager};
    use std::fs;

    #[test]
    fn observations_follow_grant_review_executor_and_shell_changes_without_paths() {
        let base =
            std::env::temp_dir().join(format!("agent-live-capabilities-{}", uuid::Uuid::new_v4()));
        let workspace = base.join("private-workspace");
        fs::create_dir_all(&workspace).unwrap();
        let mut tools = WorkToolManager::load(base.join("protected/grants.json")).unwrap();
        let missing = observe_managers(&tools, "conversation", Some("project"), true, true);
        assert_eq!(missing["workspace"]["status"], "unavailable");
        tools
            .set_project_directory("project".into(), workspace.clone())
            .unwrap();
        let open = observe_managers(&tools, "conversation", Some("project"), true, true);
        assert_eq!(open["tools"]["fs.read"]["status"], "available");
        assert_eq!(open["approval_mode"], "open");
        assert!(!open.to_string().contains("private-workspace"));
        tools
            .set_work_permission("conversation".into(), "project".into(), ApprovalMode::Human)
            .unwrap();
        let human = observe_managers(&tools, "conversation", Some("project"), true, true);
        assert_eq!(
            human["tools"]["terminal.run"]["status"],
            "requires_approval"
        );
        assert_eq!(human["approval_mode"], "human");
        let no_executor = observe_managers(&tools, "conversation", Some("project"), false, true);
        assert_eq!(no_executor["tools"]["fs.read"]["status"], "unavailable");
        let no_shell = observe_managers(&tools, "conversation", Some("project"), true, false);
        assert_eq!(no_shell["tools"]["terminal.run"]["status"], "unavailable");
        assert_eq!(no_shell["tools"]["fs.read"]["status"], "available");
        tools.suspend_project("project").unwrap();
        let suspended = observe_managers(&tools, "conversation", Some("project"), true, true);
        assert_eq!(suspended["tools"]["fs.read"]["status"], "unavailable");
        let grant = tools.project_grant("project").unwrap();
        assert_eq!(
            suspended["workspace"]["root_generation"],
            grant.root_generation
        );
        let replacement = base.join("replacement-workspace");
        fs::create_dir_all(&replacement).unwrap();
        tools
            .set_project_directory("project".into(), replacement)
            .unwrap();
        let rebound = observe_managers(&tools, "conversation", Some("project"), true, true);
        assert_eq!(rebound["workspace"]["status"], "available");
        assert_ne!(
            rebound["workspace"]["root_generation"],
            suspended["workspace"]["root_generation"]
        );
        assert!(!rebound.to_string().contains("replacement-workspace"));
        let tmp = std::env::temp_dir().canonicalize().unwrap();
        assert!(base.canonicalize().unwrap().starts_with(tmp));
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn unavailable_storage_is_not_an_active_grant() {
        let tools = WorkToolManager::disabled("unused".into(), "PRIVATE-REASON");
        let value = observe_managers(&tools, "c", Some("p"), true, true);
        assert_eq!(value["tools"]["fs.list"]["status"], "unavailable");
        assert!(!value.to_string().contains("PRIVATE-REASON"));
    }
}
