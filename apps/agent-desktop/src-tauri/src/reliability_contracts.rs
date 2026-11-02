//! Shared wire examples, exercised through production boundary types.
use serde_json::{json, Value};

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../../contracts/reliability-repair-fixtures.json"
    ))
    .unwrap()
}

#[test]
fn shared_compaction_target_and_attachment_references_survive_host_boundary() {
    let fixture = fixture();
    let target: crate::commands::context::CompactionTargetRequest =
        serde_json::from_value(fixture["compaction_target"].clone()).unwrap();
    assert_eq!(
        serde_json::to_value(target).unwrap(),
        fixture["compaction_target"]
    );
    let request: crate::domain_commands::ModelRunStartRequest = serde_json::from_value(json!({
        "content": "", "provider_profile_id": "allowed-local", "model_id": "larger-context",
        "attachment_references": fixture["attachment_references"],
        "attachments": [{"storage_key": "forged"}]
    }))
    .unwrap();
    assert!(request.attachments.is_empty());
    assert_eq!(
        serde_json::to_value(request).unwrap()["attachment_references"],
        fixture["attachment_references"]
    );
    let mut forged = fixture["attachment_references"][0].clone();
    forged["storage_key"] = json!("forged");
    assert!(serde_json::from_value::<crate::domain_commands::AttachmentReference>(forged).is_err());
}

#[test]
fn shared_search_page_includes_scan_completeness_separate_from_pagination() {
    let fixture = fixture();
    for expected in fixture["search_pages"].as_array().unwrap() {
        let page = crate::file_search::SearchPage {
            matches: serde_json::from_value(expected["matches"].clone()).unwrap(),
            inspected_files: expected["inspected_files"].as_u64().unwrap() as usize,
            next_cursor: serde_json::from_value(expected["next_cursor"].clone()).unwrap(),
            complete: expected["complete"].as_bool().unwrap(),
            truncated: expected["truncated"].as_bool().unwrap(),
            stop_reason: serde_json::from_value(expected["stop_reason"].clone()).unwrap(),
        };
        assert_eq!(serde_json::to_value(page).unwrap(), *expected);
    }
}

#[test]
fn shared_recovery_fields_survive_host_serialization_and_legacy_fields_default() {
    let fixture = fixture();
    let view: crate::agent_execution::ChangeSetView =
        serde_json::from_value(fixture["change_set"].clone()).unwrap();
    let encoded = serde_json::to_value(view).unwrap();
    for key in [
        "status",
        "execution_id",
        "recovery_error_code",
        "recovery_affected_paths",
    ] {
        assert_eq!(encoded[key], fixture["change_set"][key], "lost {key}");
    }
    let mut legacy = fixture["change_set"].clone();
    for key in [
        "execution_id",
        "recovery_error_code",
        "recovery_affected_paths",
        "plan",
    ] {
        legacy.as_object_mut().unwrap().remove(key);
    }
    legacy["status"] = json!("completed");
    let decoded: crate::agent_execution::ChangeSetView = serde_json::from_value(legacy).unwrap();
    assert!(decoded.execution_id.is_none());
    assert!(decoded.recovery_error_code.is_none());
    assert!(decoded.recovery_affected_paths.is_empty());
    assert!(decoded.plan.is_none());
    assert_eq!(decoded.status, "completed");
}

#[test]
fn shared_plan_preserves_identity_precision_dependencies_and_approval_binding() {
    let fixture = fixture();
    let view: crate::agent_execution::ChangeSetView =
        serde_json::from_value(fixture["change_set"].clone()).unwrap();
    let plan = view.plan.unwrap();
    let encoded = serde_json::to_value(&plan).unwrap();
    let expected = &fixture["change_set"]["plan"];
    for key in [
        "plan_id",
        "plan_hash",
        "policy_version",
        "root_generation",
        "effective_risk",
    ] {
        assert_eq!(encoded[key], expected[key], "lost {key}");
    }
    assert_eq!(encoded["steps"].as_array().unwrap().len(), 2);
    for (actual, expected_step) in encoded["steps"]
        .as_array()
        .unwrap()
        .iter()
        .zip(expected["steps"].as_array().unwrap())
    {
        for key in ["operation_id", "depends_on", "preconditions"] {
            assert_eq!(actual[key], expected_step[key], "lost {key}");
        }
    }
    assert_eq!(
        plan.steps[0].preconditions["."].identity.as_deref(),
        Some("fedcba9876543210:fedcba98765432100123456789abcdef")
    );
    let wire = &fixture["approval"];
    let mut approval = crate::change_set_planner::PlanApproval {
        plan_id: wire["plan_id"].as_str().unwrap().into(),
        plan_hash: wire["plan_hash"].as_str().unwrap().into(),
        approved_operation_ids: serde_json::from_value(wire["approved_operation_ids"].clone())
            .unwrap(),
    };
    approval.approved_operation_ids.reverse();
    let mutations = plan.select(&approval).unwrap();
    assert_eq!(
        mutations
            .iter()
            .map(|item| item.path.as_str())
            .collect::<Vec<_>>(),
        ["文档", "文档/notes.txt"]
    );
    approval.approved_operation_ids = vec!["write-notes".into()];
    assert!(plan
        .select(&approval)
        .unwrap_err()
        .contains("approval_missing_dependency"));
    approval.plan_hash = "forged".into();
    assert!(plan
        .select(&approval)
        .unwrap_err()
        .contains("approval_plan_mismatch"));
}
