use std::{
    collections::{BTreeMap, HashMap, HashSet},
    fs,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use aes_gcm::{
    aead::{Aead, KeyInit},
    Aes256Gcm, Nonce,
};
#[cfg(test)]
use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::{
    change_set_planner::{compile_change_set, ChangeSetPlan, PlanApproval},
    change_set_recovery::{
        write_encrypted, ExecutionJournal, JournalStore, Mutation, RecoveryOutcome,
    },
    file_access::{AuthorizedRoot, FileAccessPolicy, VerifiedReadHandle},
    file_security::{classify_mutations, OperationRisk},
    work_tools::ApprovalMode,
};

const APPROVAL_TTL: Duration = Duration::from_secs(600);
pub(crate) const MAX_READ_BYTES: usize = 1_048_576;
pub(crate) const MAX_CHANGE_BYTES: u64 = 20 * 1024 * 1024;
const MAX_RECOVERY_BYTES: u64 = 2 * 1024 * 1024 * 1024;
const UNDO_WINDOW_MS: i64 = 24 * 60 * 60 * 1_000;
const MAX_UNDO_DEPTH: u64 = 10;
const UNDO_CATALOG_FILE: &str = "undo-catalog-v1.bin";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileMutation {
    pub kind: String,
    pub path: String,
    #[serde(default)]
    pub source_path: Option<String>,
    #[serde(default)]
    pub content: Option<String>,
    #[serde(default)]
    pub old_text: Option<String>,
    #[serde(default)]
    pub new_text: Option<String>,
    #[serde(default)]
    pub replace_all: bool,
    #[serde(default)]
    pub expected_hash: Option<String>,
    #[serde(default)]
    pub expected_after_hash: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub artifact_key: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChangeSetView {
    pub id: String,
    pub operation_id: String,
    pub run_id: String,
    pub conversation_id: String,
    pub project_id: String,
    pub title: String,
    pub risk: OperationRisk,
    pub approval_mode: ApprovalMode,
    pub mutations: Vec<FileMutation>,
    #[serde(default)]
    pub plan: Option<ChangeSetPlan>,
    pub status: String,
    pub expires_at_epoch_ms: i64,
    pub recovery_point_id: Option<String>,
    #[serde(default)]
    pub execution_id: Option<String>,
    #[serde(default)]
    pub recovery_error_code: Option<String>,
    #[serde(default)]
    pub recovery_affected_paths: Vec<String>,
    #[serde(default)]
    pub created_at: Option<String>,
    #[serde(default)]
    pub undo_state: String,
    #[serde(default)]
    pub undo_sequence: Option<u64>,
    #[serde(default)]
    pub undo_expires_at: Option<String>,
    #[serde(default)]
    pub undo_block_reason: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ExecutionResult {
    pub output: String,
    pub change_set: Option<ChangeSetView>,
    pub recovery_point_id: Option<String>,
}

pub enum ChangeDisposition {
    Executed(ExecutionResult),
    NeedsHuman(ChangeSetView),
    NeedsAiReview(ChangeSetView),
}

struct PendingChangeSet {
    view: ChangeSetView,
    root: PathBuf,
    root_generation: u64,
    expires_at: Instant,
}

#[derive(Debug, Serialize, Deserialize)]
struct RecoverySnapshot {
    id: String,
    project_id: String,
    conversation_id: String,
    created_at: String,
    entries: BTreeMap<String, SnapshotEntry>,
    #[serde(default)]
    after: BTreeMap<String, SnapshotExpectation>,
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct UndoCatalog {
    #[serde(default = "undo_catalog_version")]
    version: u32,
    #[serde(default)]
    last_seen_utc_ms: i64,
    #[serde(default)]
    project_max_sequence: HashMap<String, u64>,
    #[serde(default)]
    records: HashMap<String, UndoRecord>,
}

fn undo_catalog_version() -> u32 {
    1
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct UndoRecord {
    change_set: ChangeSetView,
    root_generation: u64,
    created_at_epoch_ms: i64,
    undo_sequence: u64,
    status: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct SnapshotEntry {
    kind: String,
    content_base64: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
struct SnapshotExpectation {
    kind: String,
    sha256: Option<String>,
}

pub struct AgentExecutionManager {
    recovery_dir: PathBuf,
    protected_root: PathBuf,
    key: [u8; 32],
    disabled_reason: Option<String>,
    pending: HashMap<String, PendingChangeSet>,
    recent: Vec<ChangeSetView>,
    undo_catalog: UndoCatalog,
    document_sources: HashMap<String, (PathBuf, String)>,
}

impl AgentExecutionManager {
    pub(crate) fn for_startup(
        recovery_dir: PathBuf,
        installation_secret: &str,
        health_probe: bool,
        migration_pending: bool,
    ) -> Result<Self, String> {
        if health_probe || migration_pending {
            return Ok(Self::disabled(
                recovery_dir,
                "startup recovery is suspended",
            ));
        }
        Self::new(recovery_dir, installation_secret)
    }

    pub fn new(recovery_dir: PathBuf, installation_secret: &str) -> Result<Self, String> {
        fs::create_dir_all(&recovery_dir).map_err(|_| {
            "recovery_unavailable: Recovery storage could not be created".to_string()
        })?;
        let recovery_dir = recovery_dir.canonicalize().map_err(|_| {
            "recovery_unavailable: Recovery storage identity could not be resolved".to_string()
        })?;
        let key: [u8; 32] = Sha256::digest(installation_secret.as_bytes()).into();
        let protected_root = recovery_dir.parent().unwrap_or(&recovery_dir).to_path_buf();
        let undo_catalog = Self::load_undo_catalog(&recovery_dir, &key)?;
        let mut manager = Self {
            recovery_dir,
            protected_root,
            key,
            disabled_reason: None,
            pending: HashMap::new(),
            recent: Vec::new(),
            undo_catalog,
            document_sources: HashMap::new(),
        };
        manager.rebuild_recent_from_catalog();
        manager.refresh_journal_views()?;
        manager.cleanup_recovery()?;
        Ok(manager)
    }

    pub fn disabled(recovery_dir: PathBuf, reason: impl Into<String>) -> Self {
        let protected_root = recovery_dir.parent().unwrap_or(&recovery_dir).to_path_buf();
        Self {
            recovery_dir,
            protected_root,
            key: [0; 32],
            disabled_reason: Some(reason.into()),
            pending: HashMap::new(),
            recent: Vec::new(),
            undo_catalog: UndoCatalog::default(),
            document_sources: HashMap::new(),
        }
    }

    pub fn execute_read(
        &self,
        root: &Path,
        tool_name: &str,
        arguments: &Value,
    ) -> Result<String, String> {
        self.ensure_available()?;
        match tool_name {
            "fs.list" => self.list(root, arguments),
            #[cfg(test)]
            "fs.search" => self.search(root, arguments),
            "fs.read" => self.read(root, arguments),
            _ => Err("unsupported_tool: The requested file tool is not supported".to_string()),
        }
    }

    pub fn register_document(
        &mut self,
        key: &str,
        filename: &str,
        expected_hash: &str,
    ) -> Result<(), String> {
        if uuid::Uuid::parse_str(key).is_err()
            || Path::new(filename)
                .file_name()
                .and_then(|name| name.to_str())
                != Some(filename)
            || !matches!(
                Path::new(filename)
                    .extension()
                    .and_then(|name| name.to_str()),
                Some("docx" | "md" | "txt")
            )
        {
            return Err("document_artifact_invalid: Invalid managed document identity".into());
        }
        let directory = self.protected_root.join("document-artifacts");
        let scope = AuthorizedRoot::for_host_path(&directory, Path::new(""));
        let source =
            FileAccessPolicy.open_read(&scope, &FileAccessPolicy.validate_relative(filename)?)?;
        if source.hash(MAX_CHANGE_BYTES)? != expected_hash {
            return Err("document_artifact_changed: Managed document hash mismatch".into());
        }
        self.document_sources.insert(
            key.into(),
            (source.path().to_path_buf(), expected_hash.into()),
        );
        Ok(())
    }

    fn document_bytes(&self, key: &str) -> Result<Vec<u8>, String> {
        let (source, expected) = self
            .document_sources
            .get(key)
            .ok_or("document_artifact_unavailable: Regenerate the document before publishing")?;
        let directory = self.protected_root.join("document-artifacts");
        let scope = AuthorizedRoot::for_host_path(&directory, Path::new(""));
        let relative = source
            .strip_prefix(&directory)
            .map_err(|_| "document_artifact_invalid: Invalid managed source")?;
        let file = FileAccessPolicy.open_read(
            &scope,
            &FileAccessPolicy.validate_relative(&relative.to_string_lossy())?,
        )?;
        let bytes = file.read_bytes(MAX_CHANGE_BYTES)?;
        if format!("{:x}", Sha256::digest(&bytes)) != *expected {
            return Err(
                "document_artifact_changed: Managed document changed before publishing".into(),
            );
        }
        Ok(bytes)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn prepare_changes(
        &mut self,
        operation_id: String,
        run_id: String,
        conversation_id: String,
        project_id: String,
        root: PathBuf,
        root_generation: u64,
        mode: ApprovalMode,
        arguments: &Value,
    ) -> Result<ChangeDisposition, String> {
        self.ensure_available()?;
        self.ensure_project_recovered(&project_id)?;
        let title = arguments
            .get("title")
            .and_then(Value::as_str)
            .ok_or_else(|| "invalid_tool_call: Change title is missing".to_string())?
            .to_string();
        let raw_mutations = arguments
            .get("mutations")
            .and_then(Value::as_array)
            .ok_or_else(|| "invalid_tool_call: Change mutations are missing".to_string())?;
        let mut mutations = raw_mutations
            .iter()
            .cloned()
            .map(serde_json::from_value)
            .collect::<Result<Vec<FileMutation>, _>>()
            .map_err(|_| "invalid_tool_call: File mutation data is invalid".to_string())?;
        if mutations.is_empty() || mutations.len() > crate::change_set_planner::MAX_MUTATIONS {
            return Err("tool_limit_exceeded: A change set needs 1 to 20 mutations".to_string());
        }
        let risk = classify_mutations(raw_mutations);
        if risk == OperationRisk::Forbidden {
            return Err("forbidden_operation: This file operation is never allowed".to_string());
        }
        self.capture_expected_hashes(&root, &mut mutations)?;
        let scope = self.recovery_scope(&root, root_generation, &project_id);
        let plan = compile_change_set(&scope, &mutations)?;
        let mutations = plan
            .steps
            .iter()
            .map(|step| step.mutation.clone())
            .collect();
        let risk = plan.effective_risk;
        let view = ChangeSetView {
            id: Uuid::new_v4().to_string(),
            operation_id,
            run_id,
            conversation_id,
            project_id,
            title,
            risk,
            approval_mode: mode,
            mutations,
            plan: Some(plan),
            status: "ready".to_string(),
            expires_at_epoch_ms: chrono::Utc::now().timestamp_millis() + 600_000,
            recovery_point_id: None,
            execution_id: None,
            recovery_error_code: None,
            recovery_affected_paths: Vec::new(),
            created_at: Some(chrono::Utc::now().to_rfc3339()),
            undo_state: "unavailable".to_string(),
            undo_sequence: None,
            undo_expires_at: None,
            undo_block_reason: None,
        };
        let requires_human = match mode {
            ApprovalMode::Human => risk != OperationRisk::Routine,
            ApprovalMode::AiReview => risk == OperationRisk::High,
            ApprovalMode::Open => false,
        };
        let requires_ai = mode == ApprovalMode::AiReview && risk == OperationRisk::Medium;
        if !requires_human && !requires_ai {
            return self
                .execute_view(&root, root_generation, view)
                .map(ChangeDisposition::Executed);
        }
        let mut pending_view = view.clone();
        pending_view.status = if requires_ai {
            "reviewing".to_string()
        } else {
            "waiting_user".to_string()
        };
        self.pending.insert(
            pending_view.id.clone(),
            PendingChangeSet {
                view: pending_view.clone(),
                root,
                root_generation,
                expires_at: Instant::now() + APPROVAL_TTL,
            },
        );
        Ok(if requires_ai {
            ChangeDisposition::NeedsAiReview(pending_view)
        } else {
            ChangeDisposition::NeedsHuman(pending_view)
        })
    }

    pub fn resolve_change_set(
        &mut self,
        change_set_id: &str,
        approve: bool,
        approval: Option<&PlanApproval>,
        current_root: &Path,
        current_generation: u64,
    ) -> Result<ExecutionResult, String> {
        self.ensure_available()?;
        let pending = self.pending.get(change_set_id).ok_or_else(|| {
            "approval_missing: The approval is missing or already used".to_string()
        })?;
        if !approve {
            let mut pending = self.pending.remove(change_set_id).expect("pending checked");
            pending.view.status = "rejected".into();
            self.remember(pending.view.clone());
            return Ok(ExecutionResult {
                output: "The proposed file changes were rejected.".into(),
                change_set: Some(pending.view),
                recovery_point_id: None,
            });
        }
        self.ensure_project_recovered(&pending.view.project_id)?;
        if Instant::now() > pending.expires_at {
            return Err("approval_expired: The approval has expired".into());
        }
        if current_generation != pending.root_generation || current_root != pending.root {
            return Err("scope_changed: The project directory changed while waiting".into());
        }
        let plan = pending.view.plan.as_ref().ok_or_else(|| {
            "approval_replan_required: Prepare and approve a new plan".to_string()
        })?;
        let approval = approval.ok_or_else(|| {
            "approval_plan_required: Approval must identify the reviewed plan".to_string()
        })?;
        let mutations = plan.select(approval)?;
        let scope = self.recovery_scope(current_root, current_generation, &pending.view.project_id);
        plan.validate(&scope)?;
        let mut pending = self
            .pending
            .remove(change_set_id)
            .expect("validated pending exists");
        pending.view.mutations = mutations;
        self.execute_view(current_root, current_generation, pending.view)
    }

    pub fn pending_change_set(&self, id: &str) -> Option<&ChangeSetView> {
        self.pending.get(id).map(|item| &item.view)
    }

    pub fn mark_waiting_user(&mut self, id: &str) -> Result<ChangeSetView, String> {
        let pending = self.pending.get_mut(id).ok_or_else(|| {
            "approval_missing: The approval is missing or already used".to_string()
        })?;
        pending.view.status = "waiting_user".to_string();
        Ok(pending.view.clone())
    }

    pub fn list_change_sets(&mut self) -> Vec<ChangeSetView> {
        let now = self.observe_now();
        let mut result = self
            .pending
            .values()
            .map(|item| item.view.clone())
            .collect::<Vec<_>>();
        result.extend(
            self.recent
                .iter()
                .cloned()
                .map(|view| self.decorate_undo_state(view, now)),
        );
        result
    }

    pub fn undo_target(&mut self, change_set_id: &str) -> Result<(String, u64), String> {
        let now = self.observe_now();
        let record = self
            .undo_catalog
            .records
            .values()
            .find(|record| record.change_set.id == change_set_id)
            .ok_or_else(|| {
                "undo_recovery_unavailable: This change is not recoverable".to_string()
            })?;
        self.validate_undo_record(record, now)?;
        Ok((record.change_set.project_id.clone(), record.root_generation))
    }

    pub fn cancel_for_conversation(&mut self, conversation_id: &str) -> Vec<String> {
        let ids = self
            .pending
            .iter()
            .filter_map(|(id, item)| {
                (item.view.conversation_id == conversation_id).then_some(id.clone())
            })
            .collect::<Vec<_>>();
        let mut operations = Vec::new();
        for id in ids {
            if let Some(mut pending) = self.pending.remove(&id) {
                operations.push(pending.view.operation_id.clone());
                pending.view.status = "cancelled_scope_changed".to_string();
                self.remember(pending.view);
            }
        }
        operations
    }

    pub fn cancel_for_project(&mut self, project_id: &str) -> Vec<String> {
        let conversations = self
            .pending
            .values()
            .filter_map(|item| {
                (item.view.project_id == project_id).then_some(item.view.conversation_id.clone())
            })
            .collect::<Vec<_>>();
        let mut operations = Vec::new();
        for conversation_id in conversations {
            operations.extend(self.cancel_for_conversation(&conversation_id));
        }
        operations.sort();
        operations.dedup();
        operations
    }

    pub fn undo(
        &mut self,
        change_set_id: &str,
        root: &Path,
        current_root_generation: u64,
    ) -> Result<String, String> {
        self.ensure_available()?;
        let now = self.observe_now();
        let record = self
            .undo_catalog
            .records
            .values()
            .find(|record| record.change_set.id == change_set_id)
            .cloned()
            .ok_or_else(|| {
                "undo_recovery_unavailable: This change is not recoverable".to_string()
            })?;
        self.validate_undo_record(&record, now)?;
        if record.root_generation != current_root_generation {
            return Err(
                "undo_recovery_unavailable: The project directory changed after this operation"
                    .to_string(),
            );
        }
        let recovery_point_id =
            record
                .change_set
                .recovery_point_id
                .as_deref()
                .ok_or_else(|| {
                    "undo_recovery_unavailable: The recovery point is missing".to_string()
                })?;
        let Some(execution_id) = record.change_set.execution_id.as_deref() else {
            // V1 decrypts for diagnostics, but lacks object identity and cannot authorize destructive restoration.
            let _ = self.read_snapshot(recovery_point_id)?;
            return Err(
                "recovery_unverified: Legacy recovery lacks object identity evidence".into(),
            );
        };
        let scope =
            self.recovery_scope(root, current_root_generation, &record.change_set.project_id);
        let journal = match self
            .journal_store()
            .recover_execution(execution_id, &scope, true)
        {
            Ok(journal) => journal,
            Err(error) => {
                self.reconcile_recovery_error(execution_id, Some(record.change_set.clone()));
                return Err(error);
            }
        };
        let outcome = journal.outcome();
        let view = self.reconcile_journal(&journal, true);
        if let Err(error) = self.persist_undo_catalog() {
            self.reconcile_recovery_error(execution_id, Some(view));
            return Err(error);
        }
        match outcome {
            RecoveryOutcome::RolledBack => {
                Ok(format!("Restored {} operations", journal.steps.len()))
            }
            RecoveryOutcome::RollbackConflicted => {
                Err("undo_conflict: rollback_incomplete; external changes were preserved".into())
            }
            _ => Err("recovery_required: Undo needs verified recovery".into()),
        }
    }

    fn journal_store(&self) -> JournalStore {
        JournalStore::new(self.recovery_dir.clone(), self.key)
    }
    fn recovery_scope(&self, root: &Path, generation: u64, project_id: &str) -> AuthorizedRoot {
        let mut scope = self.access_root(root);
        scope.root_generation = generation;
        scope.project_id = project_id.into();
        scope
    }
    fn ensure_project_recovered(&self, project_id: &str) -> Result<(), String> {
        let unresolved_live = self.recent.iter().any(|view| {
            view.project_id == project_id
                && matches!(
                    view.status.as_str(),
                    "recovery_required" | "rollback_conflicted"
                )
        });
        // The journal is authority even when a bounded recent list or stale catalog has no matching entry.
        let unresolved_durable = self.journal_store().journals()?.iter().any(|journal| {
            journal.view.project_id == project_id
                && !matches!(
                    journal.outcome(),
                    RecoveryOutcome::Completed | RecoveryOutcome::RolledBack
                )
        });
        if unresolved_live || unresolved_durable {
            return Err(
                "recovery_required: Resolve the previous file recovery before new changes".into(),
            );
        }
        Ok(())
    }
    fn reconcile_journal(
        &mut self,
        journal: &ExecutionJournal,
        intentional_undo: bool,
    ) -> ChangeSetView {
        let mut view = journal.public_view();
        if let Some(record) = view
            .recovery_point_id
            .as_ref()
            .and_then(|id| self.undo_catalog.records.get_mut(id))
        {
            // Journal status wins; catalog sequence/time/scope metadata still defines the undo window.
            view.created_at = record.change_set.created_at.clone();
            view.undo_sequence = Some(record.undo_sequence);
            view.undo_expires_at = record.change_set.undo_expires_at.clone();
            if journal.outcome() == RecoveryOutcome::RolledBack
                && (intentional_undo
                    || record.status == "undone"
                    || record.change_set.status == "undone")
            {
                view.status = "undone".into();
                view.undo_state = "already_undone".into();
            }
            record.status = view.status.clone();
            record.change_set = view.clone();
        }
        self.remember(view.clone());
        view
    }
    fn reconcile_recovery_error(&mut self, execution_id: &str, fallback: Option<ChangeSetView>) {
        match self.journal_store().read(execution_id) {
            Ok(journal) => {
                self.reconcile_journal(&journal, false);
            }
            Err(_) => {
                let previous = fallback
                    .or_else(|| self.recent.iter().find(|v| v.id == execution_id).cloned())
                    .or_else(|| {
                        self.undo_catalog
                            .records
                            .values()
                            .find(|r| r.change_set.id == execution_id)
                            .map(|r| r.change_set.clone())
                    });
                if let Some(mut view) = previous {
                    view.status = "recovery_required".into();
                    view.undo_state = "unavailable".into();
                    view.undo_block_reason = Some("recovery_required".into());
                    view.recovery_error_code = Some("recovery_required".into());
                    view.recovery_affected_paths = view
                        .mutations
                        .iter()
                        .flat_map(|m| std::iter::once(m.path.clone()).chain(m.source_path.clone()))
                        .collect();
                    view.recovery_affected_paths.sort();
                    view.recovery_affected_paths.dedup();
                    if let Some(record) = view
                        .recovery_point_id
                        .as_ref()
                        .and_then(|id| self.undo_catalog.records.get_mut(id))
                    {
                        record.status = view.status.clone();
                        record.change_set = view.clone();
                    }
                    self.remember(view);
                }
            }
        }
        // A persistence error must not roll back the conservative live state.
        let _ = self.persist_undo_catalog();
    }
    fn refresh_journal_views(&mut self) -> Result<(), String> {
        for journal in self.journal_store().journals()? {
            self.reconcile_journal(&journal, false);
        }
        self.persist_undo_catalog()
    }
    pub fn recovery_target(&self, change_set_id: &str) -> Result<String, String> {
        self.ensure_available()?;
        Ok(self.journal_store().read(change_set_id)?.view.project_id)
    }
    pub fn recover_execution(
        &mut self,
        execution_id: &str,
        access_policy: &AuthorizedRoot,
    ) -> Result<RecoveryOutcome, String> {
        self.ensure_available()?;
        let journal =
            match self
                .journal_store()
                .recover_execution(execution_id, access_policy, false)
            {
                Ok(journal) => journal,
                Err(error) => {
                    self.reconcile_recovery_error(execution_id, None);
                    return Err(error);
                }
            };
        let view = self.reconcile_journal(&journal, false);
        if let Err(error) = self.persist_undo_catalog() {
            self.reconcile_recovery_error(execution_id, Some(view));
            return Err(error);
        }
        Ok(journal.outcome())
    }
    pub fn recover_change_set(
        &mut self,
        id: &str,
        root: &Path,
        generation: u64,
    ) -> Result<ChangeSetView, String> {
        let project_id = self.recovery_target(id)?;
        self.recover_execution(id, &self.recovery_scope(root, generation, &project_id))?;
        self.recent
            .iter()
            .find(|v| v.id == id)
            .cloned()
            .ok_or_else(|| "recovery_unavailable: Recovery record is missing".into())
    }
    fn recovery_failed(
        &mut self,
        journal: &ExecutionJournal,
        scope: &AuthorizedRoot,
        error: String,
    ) -> String {
        match self
            .journal_store()
            .recover_execution(&journal.execution_id, scope, true)
        {
            Ok(recovered) => {
                let outcome = recovered.outcome();
                self.reconcile_journal(&recovered, false);
                let _ = self.persist_undo_catalog();
                if outcome == RecoveryOutcome::RolledBack {
                    error
                } else {
                    format!(
                        "{}: File changes need recovery",
                        outcome.error_code().unwrap_or("recovery_required")
                    )
                }
            }
            Err(_) => {
                self.reconcile_recovery_error(&journal.execution_id, Some(journal.public_view()));
                "recovery_required: File changes need recovery".into()
            }
        }
    }

    fn execute_view(
        &mut self,
        root: &Path,
        root_generation: u64,
        mut view: ChangeSetView,
    ) -> Result<ExecutionResult, String> {
        self.ensure_project_recovered(&view.project_id)?;
        let scope = self.recovery_scope(root, root_generation, &view.project_id);
        let plan = view.plan.as_ref().ok_or_else(|| {
            "approval_replan_required: Prepare and approve a new plan".to_string()
        })?;
        plan.validate(&scope)?;
        plan.execution_hash(&view.mutations)?;
        let mutations = view.mutations.iter().map(|mutation| {
            let bytes = match mutation.kind.as_str() {
                "create_text" if mutation.artifact_key.is_some() => self.document_bytes(mutation.artifact_key.as_deref().unwrap())?,
                "patch_text" => self.render_patch(root, &mutation.path, mutation)?.into_bytes(),
                "copy" => self.open_file(root, mutation.source_path.as_deref().unwrap_or_default())?.read_bytes(MAX_CHANGE_BYTES)?,
                _ => mutation.content.as_deref().unwrap_or_default().as_bytes().to_vec(),
            };
            if mutation.kind != "move" && mutation.expected_after_hash.as_deref().is_some_and(|expected| expected != format!("{:x}", Sha256::digest(&bytes))) {
                return Err("file_changed_since_read: Prepared content no longer matches the approved change".into());
            }
            Ok(Mutation { kind: mutation.kind.clone(), path: mutation.path.clone(), source_path: mutation.source_path.clone(), bytes, expected_hash: mutation.expected_hash.clone() })
        }).collect::<Result<Vec<_>, String>>()?;
        let store = self.journal_store();
        let mut journal = store.prepare(&scope, &view, &mutations)?;
        let recovery_id = journal.steps[0].recovery_point_id.clone();
        if let Err(error) = store.execute(&mut journal, &scope, &mutations) {
            return Err(self.recovery_failed(&journal, &scope, error));
        }
        view.status = "completed".into();
        view.execution_id = Some(journal.execution_id.clone());
        view.recovery_point_id = Some(recovery_id.clone());
        let now = self.observe_now();
        let previous_max_sequence = self
            .undo_catalog
            .project_max_sequence
            .get(&view.project_id)
            .copied()
            .unwrap_or(0);
        let sequence = previous_max_sequence.saturating_add(1);
        view.created_at = Some(epoch_ms_to_rfc3339(now));
        view.undo_state = "available".to_string();
        view.undo_sequence = Some(sequence);
        view.undo_expires_at = Some(epoch_ms_to_rfc3339(now.saturating_add(UNDO_WINDOW_MS)));
        self.undo_catalog
            .project_max_sequence
            .insert(view.project_id.clone(), sequence);
        self.undo_catalog.records.insert(
            recovery_id.clone(),
            UndoRecord {
                change_set: sanitize_change_set_for_catalog(&view),
                root_generation,
                created_at_epoch_ms: now,
                undo_sequence: sequence,
                status: "completed".to_string(),
            },
        );
        if let Err(error) = self.persist_undo_catalog() {
            self.undo_catalog.records.remove(&recovery_id);
            if previous_max_sequence == 0 {
                self.undo_catalog
                    .project_max_sequence
                    .remove(&view.project_id);
            } else {
                self.undo_catalog
                    .project_max_sequence
                    .insert(view.project_id.clone(), previous_max_sequence);
            }
            return Err(self.recovery_failed(&journal, &scope, error));
        }
        journal.view = sanitize_change_set_for_catalog(&view);
        if let Err(error) = store.save(&journal) {
            // Disk still has a completed receipt; no forward replay is needed. Surface persistence failure.
            return Err(self.recovery_failed(&journal, &scope, error));
        }

        let output = json!({
            "changed_files": view.mutations.len(),
            "recovery_point_id": recovery_id,
            "paths": view.mutations.iter().map(|item| item.path.as_str()).collect::<Vec<_>>(),
        })
        .to_string();
        self.remember(view.clone());
        let _ = self.cleanup_recovery();
        Ok(ExecutionResult {
            output,
            change_set: Some(view),
            recovery_point_id: Some(recovery_id),
        })
    }

    pub(crate) fn access_root(&self, root: &Path) -> AuthorizedRoot {
        AuthorizedRoot::for_host_path(root, &self.protected_root)
    }
    fn open_file(&self, root: &Path, relative: &str) -> Result<VerifiedReadHandle, String> {
        let policy = FileAccessPolicy;
        policy.open_read(
            &self.access_root(root),
            &policy.validate_relative(relative)?,
        )
    }
    fn open_optional(
        &self,
        root: &Path,
        relative: &str,
    ) -> Result<Option<VerifiedReadHandle>, String> {
        match self.open_file(root, relative) {
            Ok(file) => Ok(Some(file)),
            Err(error) if error.starts_with("path_not_found:") => Ok(None),
            Err(error) => Err(error),
        }
    }
    #[cfg(test)]
    fn target_path(&self, root: &Path, relative: &str) -> Result<PathBuf, String> {
        let policy = FileAccessPolicy;
        policy.target_path(
            &self.access_root(root),
            &policy.validate_relative(relative)?,
        )
    }
    fn list(&self, root: &Path, arguments: &Value) -> Result<String, String> {
        let path = arguments.get("path").and_then(Value::as_str).unwrap_or(".");
        let limit = arguments
            .get("limit")
            .and_then(Value::as_u64)
            .unwrap_or(200)
            .min(1_000) as usize;
        let offset = arguments
            .get("cursor")
            .and_then(Value::as_str)
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(0);
        let directory = self.open_file(root, path)?;
        let mut entries = Vec::new();
        for name in directory.entries()? {
            let child = if path == "." {
                name.clone()
            } else {
                format!("{path}/{name}")
            };
            let Ok(file) = self.open_file(root, &child) else {
                continue;
            };
            entries.push(if file.is_dir() {
                format!("{name}/")
            } else {
                name
            });
        }
        entries.sort_by_key(|item| item.to_ascii_lowercase());
        let page = entries
            .into_iter()
            .skip(offset)
            .take(limit)
            .collect::<Vec<_>>();
        Ok(json!({"entries": page, "next_cursor": if page.len() == limit { Some((offset + page.len()).to_string()) } else { None }}).to_string())
    }

    fn read(&self, root: &Path, arguments: &Value) -> Result<String, String> {
        let relative = required_string(arguments, "path")?;
        let offset = arguments.get("offset").and_then(Value::as_u64).unwrap_or(0) as usize;
        let length = arguments
            .get("length")
            .and_then(Value::as_u64)
            .unwrap_or(MAX_READ_BYTES as u64)
            .min(MAX_READ_BYTES as u64) as usize;
        let file = self.open_file(root, relative)?;
        let bytes = file.read_bytes(MAX_CHANGE_BYTES)?;
        let text = String::from_utf8(bytes)
            .map_err(|_| "file_not_utf8: Only UTF-8 text files can be read".to_string())?;
        if offset > text.len() || !text.is_char_boundary(offset) {
            return Err(
                "invalid_read_range: The byte offset is outside a UTF-8 boundary".to_string(),
            );
        }
        let mut end = offset.saturating_add(length).min(text.len());
        while end > offset && !text.is_char_boundary(end) {
            end -= 1;
        }
        Ok(json!({
            "path": relative,
            "offset": offset,
            "content": &text[offset..end],
            "next_offset": if end < text.len() { Some(end) } else { None },
            "sha256": format!("{:x}", Sha256::digest(text.as_bytes())),
        })
        .to_string())
    }

    #[cfg(test)]
    fn search(&self, root: &Path, arguments: &Value) -> Result<String, String> {
        crate::file_search::search_for_test(self.access_root(root), arguments)
    }

    fn capture_expected_hashes(
        &self,
        root: &Path,
        mutations: &mut [FileMutation],
    ) -> Result<(), String> {
        for mutation in mutations {
            mutation.path = crate::change_set_planner::normalize(&mutation.path)?;
            mutation.source_path = mutation
                .source_path
                .as_deref()
                .map(crate::change_set_planner::normalize)
                .transpose()?;
            FileAccessPolicy.validate_planned_target(
                &self.access_root(root),
                &FileAccessPolicy.validate_relative(&mutation.path)?,
            )?;
            let before = if matches!(mutation.kind.as_str(), "copy" | "move") {
                Some(self.open_file(root, mutation.source_path.as_deref().unwrap_or_default())?)
            } else {
                self.open_optional(root, &mutation.path)?
            }
            .filter(VerifiedReadHandle::is_file);
            if before
                .as_ref()
                .is_some_and(|file| file.len() > MAX_CHANGE_BYTES)
            {
                return Err(
                    "tool_limit_exceeded: One changed file exceeds the 20 MiB limit".to_string(),
                );
            }
            let before_hash = before
                .as_ref()
                .map(|file| file.hash(MAX_CHANGE_BYTES))
                .transpose()?;
            if mutation.expected_hash.is_none() {
                mutation.expected_hash = before_hash.clone();
            }
            mutation.expected_after_hash = match mutation.kind.as_str() {
                "create_text" if mutation.artifact_key.is_some() => Some(format!(
                    "{:x}",
                    Sha256::digest(self.document_bytes(mutation.artifact_key.as_deref().unwrap())?)
                )),
                "create_text" | "overwrite_text" => Some(format!(
                    "{:x}",
                    Sha256::digest(mutation.content.as_deref().unwrap_or_default().as_bytes())
                )),
                "patch_text" => Some(format!(
                    "{:x}",
                    Sha256::digest(
                        self.render_patch(root, &mutation.path, mutation)?
                            .as_bytes()
                    )
                )),
                "copy" | "move" => before_hash,
                _ => None,
            };
        }
        Ok(())
    }

    fn render_patch(
        &self,
        root: &Path,
        relative: &str,
        mutation: &FileMutation,
    ) -> Result<String, String> {
        let file = self.open_file(root, relative)?;
        let current = String::from_utf8(file.read_bytes(MAX_CHANGE_BYTES)?)
            .map_err(|_| "file_not_utf8: Text patches require a UTF-8 file".to_string())?;
        let old = mutation
            .old_text
            .as_deref()
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                "invalid_tool_call: A text patch requires non-empty old_text".to_string()
            })?;
        let new = mutation
            .new_text
            .as_deref()
            .ok_or_else(|| "invalid_tool_call: A text patch requires new_text".to_string())?;
        let occurrences = current.matches(old).count();
        if occurrences == 0 {
            return Err(
                "file_changed_since_read: The text to replace is no longer present".to_string(),
            );
        }
        if occurrences > 1 && !mutation.replace_all {
            return Err(
                "ambiguous_text_patch: old_text occurs more than once; provide more context or set replace_all"
                    .to_string(),
            );
        }
        Ok(if mutation.replace_all {
            current.replace(old, new)
        } else {
            current.replacen(old, new, 1)
        })
    }

    fn read_snapshot(&self, id: &str) -> Result<RecoverySnapshot, String> {
        let bytes = fs::read(self.recovery_dir.join(format!("{id}.recovery")))
            .map_err(|_| "recovery_not_found: Recovery point does not exist".to_string())?;
        if bytes.len() < 13 {
            return Err("recovery_damaged: Recovery point is damaged".to_string());
        }
        let cipher = Aes256Gcm::new_from_slice(&self.key)
            .map_err(|_| "recovery_unavailable: Recovery encryption is unavailable".to_string())?;
        let plaintext = cipher
            .decrypt(Nonce::from_slice(&bytes[..12]), &bytes[12..])
            .map_err(|_| "recovery_damaged: Recovery point cannot be decrypted".to_string())?;
        serde_json::from_slice(&plaintext)
            .map_err(|_| "recovery_damaged: Recovery point is invalid".to_string())
    }

    #[cfg(test)]
    fn restore_snapshot(&self, root: &Path, snapshot: &RecoverySnapshot) -> Result<(), String> {
        for path in snapshot.entries.keys() {
            self.target_path(root, path)?;
        }
        Err("recovery_unverified: Legacy recovery lacks object identity evidence".into())
    }

    fn cleanup_recovery(&mut self) -> Result<(), String> {
        let retained = self
            .journal_store()
            .journals()?
            .into_iter()
            .flat_map(|j| j.steps.into_iter().map(|s| s.recovery_point_id))
            .collect::<HashSet<_>>();
        let mut files = fs::read_dir(&self.recovery_dir)
            .map_err(|_| "recovery_unavailable: Recovery directory could not be read".to_string())?
            .filter_map(Result::ok)
            .filter_map(|entry| {
                if entry.path().extension().and_then(|value| value.to_str()) != Some("recovery") {
                    return None;
                }
                if entry
                    .path()
                    .file_stem()
                    .and_then(|s| s.to_str())
                    .is_some_and(|id| retained.contains(id))
                {
                    return None;
                }
                let metadata = entry.metadata().ok()?;
                Some((entry.path(), metadata.modified().ok()?, metadata.len()))
            })
            .collect::<Vec<_>>();
        files.sort_by_key(|(_, modified, _)| *modified);
        let cutoff = std::time::SystemTime::now() - Duration::from_secs(30 * 24 * 60 * 60);
        let mut total = files.iter().map(|(_, _, size)| size).sum::<u64>();
        for (path, modified, size) in files {
            if (modified < cutoff || total > MAX_RECOVERY_BYTES) && fs::remove_file(path).is_ok() {
                total = total.saturating_sub(size);
            }
        }
        Ok(())
    }

    fn load_undo_catalog(recovery_dir: &Path, key: &[u8; 32]) -> Result<UndoCatalog, String> {
        let path = recovery_dir.join(UNDO_CATALOG_FILE);
        let backup = recovery_dir.join("undo-catalog-v1.backup");
        if !path.exists() && backup.exists() {
            fs::rename(&backup, &path).map_err(|_| {
                "undo_catalog_unavailable: Interrupted undo history update could not be recovered"
                    .to_string()
            })?;
        }
        if !path.exists() {
            return Ok(UndoCatalog {
                version: undo_catalog_version(),
                ..UndoCatalog::default()
            });
        }
        let result = (|| {
            let bytes = fs::read(&path).map_err(|_| {
                "undo_catalog_unavailable: Undo history could not be read".to_string()
            })?;
            if bytes.len() < 13 {
                return Err("undo_catalog_damaged: Undo history is damaged".to_string());
            }
            let cipher = Aes256Gcm::new_from_slice(key).map_err(|_| {
                "undo_catalog_unavailable: Undo encryption is unavailable".to_string()
            })?;
            let plaintext = cipher
                .decrypt(Nonce::from_slice(&bytes[..12]), &bytes[12..])
                .map_err(|_| {
                    "undo_catalog_damaged: Undo history cannot be decrypted".to_string()
                })?;
            let catalog: UndoCatalog = serde_json::from_slice(&plaintext)
                .map_err(|_| "undo_catalog_damaged: Undo history is invalid".to_string())?;
            if catalog.version != undo_catalog_version() {
                return Err(
                    "undo_catalog_damaged: Undo history has an unsupported version".to_string(),
                );
            }
            Ok(catalog)
        })();
        match result {
            Ok(catalog) => Ok(catalog),
            Err(_) => {
                let damaged = recovery_dir.join(format!(
                    "undo-catalog-damaged-{}.bin",
                    chrono::Utc::now().timestamp_millis()
                ));
                fs::rename(&path, damaged).map_err(|_| {
                    "undo_catalog_unavailable: Damaged undo history could not be preserved"
                        .to_string()
                })?;
                Ok(UndoCatalog {
                    version: undo_catalog_version(),
                    last_seen_utc_ms: chrono::Utc::now().timestamp_millis(),
                    ..UndoCatalog::default()
                })
            }
        }
    }

    fn persist_undo_catalog(&self) -> Result<(), String> {
        if crate::profile_migration::blocks_core() {
            return Err("migration_activation_in_progress".into());
        }
        write_encrypted(
            &self.recovery_dir.join(UNDO_CATALOG_FILE),
            &self.key,
            &self.undo_catalog,
        )
    }

    fn rebuild_recent_from_catalog(&mut self) {
        let mut records = self
            .undo_catalog
            .records
            .values()
            .cloned()
            .collect::<Vec<_>>();
        records.sort_by_key(|record| std::cmp::Reverse(record.created_at_epoch_ms));
        self.recent = records
            .into_iter()
            .take(100)
            .map(|record| record.change_set)
            .collect();
    }

    fn observe_now(&mut self) -> i64 {
        let system_now = chrono::Utc::now().timestamp_millis();
        let effective = system_now.max(self.undo_catalog.last_seen_utc_ms);
        if effective > self.undo_catalog.last_seen_utc_ms {
            self.undo_catalog.last_seen_utc_ms = effective;
            let _ = self.persist_undo_catalog();
        }
        effective
    }

    fn decorate_undo_state(&self, mut view: ChangeSetView, now: i64) -> ChangeSetView {
        if matches!(
            view.status.as_str(),
            "recovery_required" | "rollback_conflicted" | "rolled_back"
        ) {
            view.undo_state = if view.status == "rollback_conflicted" {
                "conflicted"
            } else {
                "unavailable"
            }
            .into();
            view.undo_block_reason = view.recovery_error_code.clone();
            return view;
        }
        let Some(recovery_id) = view.recovery_point_id.as_deref() else {
            view.undo_state = "unavailable".to_string();
            return view;
        };
        let Some(record) = self.undo_catalog.records.get(recovery_id) else {
            view.undo_state = "unavailable".to_string();
            view.undo_block_reason = Some("undo_recovery_unavailable".to_string());
            return view;
        };
        view.undo_sequence = Some(record.undo_sequence);
        view.undo_expires_at = Some(epoch_ms_to_rfc3339(
            record.created_at_epoch_ms.saturating_add(UNDO_WINDOW_MS),
        ));
        view.undo_state = self.undo_state_for(record, now).to_string();
        view.undo_block_reason = (view.undo_state != "available")
            .then(|| undo_state_error_code(&view.undo_state).to_string());
        if view.undo_state == "legacy_unverified" {
            view.undo_state = "unavailable".into();
        }
        view
    }

    fn undo_state_for(&self, record: &UndoRecord, now: i64) -> &'static str {
        if record.change_set.execution_id.is_none() && record.status == "completed" {
            return "legacy_unverified";
        }
        if record.status == "undone" || record.change_set.status == "undone" {
            return "already_undone";
        }
        if matches!(
            record.status.as_str(),
            "conflicted" | "rollback_conflicted" | "recovery_required"
        ) {
            return "conflicted";
        }
        let recovery_available = record
            .change_set
            .recovery_point_id
            .as_deref()
            .is_some_and(|id| self.recovery_dir.join(format!("{id}.recovery")).is_file());
        if !recovery_available {
            return "unavailable";
        }
        let max_sequence = self
            .undo_catalog
            .project_max_sequence
            .get(&record.change_set.project_id)
            .copied()
            .unwrap_or(record.undo_sequence);
        if max_sequence.saturating_sub(record.undo_sequence) >= MAX_UNDO_DEPTH {
            return "expired_depth_window";
        }
        if now >= record.created_at_epoch_ms.saturating_add(UNDO_WINDOW_MS) {
            return "expired_time_window";
        }
        if self.recent.iter().any(|v| {
            v.project_id == record.change_set.project_id
                && matches!(
                    v.status.as_str(),
                    "recovery_required" | "rollback_conflicted"
                )
        }) {
            return "blocked_by_pending_change";
        }
        if self
            .pending
            .values()
            .any(|pending| pending.view.project_id == record.change_set.project_id)
        {
            return "blocked_by_pending_change";
        }
        let newest_active = self
            .undo_catalog
            .records
            .values()
            .filter(|candidate| {
                candidate.change_set.project_id == record.change_set.project_id
                    && candidate.status == "completed"
            })
            .map(|candidate| candidate.undo_sequence)
            .max();
        if newest_active != Some(record.undo_sequence) {
            return "blocked_by_newer_change";
        }
        "available"
    }

    fn validate_undo_record(&self, record: &UndoRecord, now: i64) -> Result<(), String> {
        let state = self.undo_state_for(record, now);
        if state == "available" {
            return Ok(());
        }
        Err(format!(
            "{}: This change cannot be undone in its current state",
            undo_state_error_code(state)
        ))
    }

    fn remember(&mut self, view: ChangeSetView) {
        self.recent.retain(|previous| previous.id != view.id);
        self.recent.insert(0, view);
        let mut resolved = 0;
        self.recent.retain(|v| {
            if matches!(
                v.status.as_str(),
                "recovery_required" | "rollback_conflicted"
            ) {
                true
            } else {
                resolved += 1;
                resolved <= 100
            }
        });
    }

    pub(crate) fn ensure_available(&self) -> Result<(), String> {
        if crate::profile_migration::blocks_core() {
            return Err("migration_activation_in_progress".into());
        }
        if self.disabled_reason.is_some() {
            return Err(
                "tool_system_unavailable: Secure file recovery is unavailable; pure chat remains available"
                    .to_string(),
            );
        }
        Ok(())
    }
}

fn epoch_ms_to_rfc3339(epoch_ms: i64) -> String {
    chrono::DateTime::<chrono::Utc>::from_timestamp_millis(epoch_ms)
        .unwrap_or_else(chrono::Utc::now)
        .to_rfc3339()
}

fn sanitize_change_set_for_catalog(view: &ChangeSetView) -> ChangeSetView {
    let mut sanitized = view.clone();
    for mutation in &mut sanitized.mutations {
        mutation.content = None;
        mutation.old_text = None;
        mutation.new_text = None;
    }
    if let Some(plan) = &mut sanitized.plan {
        plan.redact();
    }
    sanitized
}

fn undo_state_error_code(state: &str) -> &'static str {
    match state {
        "legacy_unverified" => "recovery_unverified",
        "blocked_by_newer_change" => "undo_order_conflict",
        "blocked_by_pending_change" => "undo_pending_change",
        "expired_time_window" => "undo_window_expired",
        "expired_depth_window" => "undo_depth_exceeded",
        "already_undone" => "undo_recovery_unavailable",
        _ => "undo_recovery_unavailable",
    }
}

fn required_string<'a>(value: &'a Value, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|item| !item.is_empty())
        .ok_or_else(|| format!("invalid_tool_call: {key} is required"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir() -> PathBuf {
        let path = std::env::temp_dir().join(format!("agent-execution-{}", Uuid::new_v4()));
        fs::create_dir_all(&path).unwrap();
        path.canonicalize().unwrap()
    }

    #[test]
    fn managed_document_publication_keeps_binary_out_of_change_json_and_checks_hash() {
        let root = temp_dir();
        let private = temp_dir();
        let store = private.join("document-artifacts");
        fs::create_dir(&store).unwrap();
        let private_alias = std::env::temp_dir().join(private.file_name().unwrap());
        let mut manager =
            AgentExecutionManager::new(private_alias.join("points"), "document-secret").unwrap();
        let key = Uuid::new_v4().to_string();
        let filename = format!("{key}.docx");
        let bytes = b"PK\x00managed-document\xff";
        fs::write(store.join(&filename), bytes).unwrap();
        let hash = format!("{:x}", Sha256::digest(bytes));
        manager.register_document(&key, &filename, &hash).unwrap();
        let arguments = json!({"title":"保存模板", "mutations":[{"kind":"create_text","path":"模板.docx","artifact_key":key}]});
        let disposition = manager
            .prepare_changes(
                "document-op".into(),
                "run".into(),
                "work".into(),
                "project".into(),
                root.clone(),
                1,
                ApprovalMode::Open,
                &arguments,
            )
            .unwrap();
        assert!(matches!(disposition, ChangeDisposition::Executed(_)));
        assert_eq!(fs::read(root.join("模板.docx")).unwrap(), bytes);
        assert!(!arguments.to_string().contains("managed-document"));
        fs::write(store.join(&filename), b"changed").unwrap();
        let changed = json!({"title":"保存模板", "mutations":[{"kind":"create_text","path":"changed.docx","artifact_key":key}]});
        assert!(manager
            .prepare_changes(
                "other-op".into(),
                "run".into(),
                "work".into(),
                "project".into(),
                root.clone(),
                1,
                ApprovalMode::Open,
                &changed
            )
            .is_err());
        assert!(!root.join("changed.docx").exists());
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(private).unwrap();
    }

    #[test]
    fn change_set_recovery_blind_rollback_preserves_external_edit() {
        let root = temp_dir();
        let recovery = temp_dir();
        let manager = AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        fs::write(root.join("note.txt"), "before").unwrap();
        let snapshot = RecoverySnapshot {
            id: "synthetic".into(),
            project_id: "p".into(),
            conversation_id: "c".into(),
            created_at: "now".into(),
            entries: BTreeMap::from([(
                "note.txt".into(),
                SnapshotEntry {
                    kind: "file".into(),
                    content_base64: Some(BASE64.encode("before")),
                },
            )]),
            after: BTreeMap::new(),
        };
        fs::write(root.join("note.txt"), "external edit").unwrap();
        let _ = manager.restore_snapshot(&root, &snapshot);
        assert_eq!(
            fs::read_to_string(root.join("note.txt")).unwrap(),
            "external edit"
        );
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn default_work_mode_executes_multifile_page_and_preserves_undo() {
        let fixture = temp_dir();
        let root = fixture.join("project");
        fs::create_dir(&root).unwrap();
        fs::write(root.join("README.md"), "original notes").unwrap();
        let mut tools =
            crate::work_tools::WorkToolManager::load(fixture.join("state/grants.json")).unwrap();
        tools
            .set_project_directory("project".into(), root.clone())
            .unwrap();
        let permission = tools.work_permission("work", "project").unwrap();
        let mut manager =
            AgentExecutionManager::new(fixture.join("app-data/recovery"), "synthetic").unwrap();
        let arguments = json!({"title":"Create synthetic teaching page", "mutations":[
            {"kind":"mkdir", "path":"css"},
            {"kind":"mkdir", "path":"js"},
            {"kind":"create_text", "path":"index.html", "content":"<!doctype html><script src='js/main.js'></script>"},
            {"kind":"create_text", "path":"css/style.css", "content":"body { color: navy; }"},
            {"kind":"create_text", "path":"js/net.js", "content":"const network = {};"},
            {"kind":"create_text", "path":"js/data.js", "content":"const data = [];"},
            {"kind":"create_text", "path":"js/viz.js", "content":"function draw() {}"},
            {"kind":"create_text", "path":"js/main.js", "content":"draw();"},
            {"kind":"overwrite_text", "path":"README.md", "content":"Open index.html"}
        ]});
        let result = manager
            .prepare_changes(
                "op".into(),
                "run".into(),
                "work".into(),
                "project".into(),
                root.clone(),
                permission.root_generation,
                permission.mode,
                &arguments,
            )
            .unwrap();
        let ChangeDisposition::Executed(result) = result else {
            panic!("Default work must execute without an approval prompt")
        };
        for mutation in arguments["mutations"].as_array().unwrap() {
            if mutation["kind"] == "mkdir" {
                continue;
            }
            assert_eq!(
                fs::read_to_string(root.join(mutation["path"].as_str().unwrap())).unwrap(),
                mutation["content"].as_str().unwrap()
            );
        }
        manager
            .undo(
                &result.change_set.unwrap().id,
                &root,
                permission.root_generation,
            )
            .unwrap();
        assert_eq!(
            fs::read_to_string(root.join("README.md")).unwrap(),
            "original notes"
        );
        assert!(!root.join("index.html").exists());
        assert!(!root.join("js").exists());
        assert!(!root.join("css").exists());
        fs::remove_dir_all(fixture).unwrap();
    }

    #[test]
    fn file_access_copy_move_and_undo_use_verified_sources_and_targets() {
        let root = temp_dir();
        let recovery = temp_dir();
        fs::write(root.join("source.txt"), "synthetic text").unwrap();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        for kind in ["copy", "move"] {
            let result = manager.prepare_changes("op".into(), "run".into(), "work".into(), "project".into(), root.clone(), 1, ApprovalMode::Open,
                &json!({"title":"synthetic change", "mutations":[{"kind":kind, "source_path":"source.txt", "path":"target.txt"}]})).unwrap();
            let ChangeDisposition::Executed(result) = result else {
                panic!("open-mode operation must execute")
            };
            assert_eq!(
                fs::read_to_string(root.join("target.txt")).unwrap(),
                "synthetic text"
            );
            manager
                .undo(&result.change_set.unwrap().id, &root, 1)
                .unwrap();
            assert!(!root.join("target.txt").exists());
            assert_eq!(
                fs::read_to_string(root.join("source.txt")).unwrap(),
                "synthetic text"
            );
        }
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn file_access_recovery_cannot_authorize_sensitive_or_protected_paths() {
        let root = temp_dir();
        let recovery = root.join("state/points");
        let manager = AgentExecutionManager::new(recovery, "test-secret").unwrap();
        fs::create_dir(root.join("credentials")).unwrap();
        for relative in ["credentials/data.txt", "state/data.txt"] {
            fs::write(root.join(relative), "synthetic original").unwrap();
            let snapshot = RecoverySnapshot {
                id: "synthetic".into(),
                project_id: "p".into(),
                conversation_id: "c".into(),
                created_at: "synthetic".into(),
                entries: BTreeMap::from([(
                    relative.into(),
                    SnapshotEntry {
                        kind: "file".into(),
                        content_base64: Some(BASE64.encode("changed")),
                    },
                )]),
                after: BTreeMap::new(),
            };
            assert!(manager.restore_snapshot(&root, &snapshot).is_err());
            assert_eq!(
                fs::read_to_string(root.join(relative)).unwrap(),
                "synthetic original"
            );
        }
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn file_security_snapshots_and_writes_reject_nested_credentials() {
        let root = temp_dir();
        let recovery = temp_dir();
        fs::create_dir(root.join("credentials")).unwrap();
        fs::write(root.join("credentials/data.txt"), "synthetic marker").unwrap();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        assert!(crate::change_set_recovery::capture(
            &manager.access_root(&root),
            "credentials/data.txt"
        )
        .is_err());
        let result = manager.prepare_changes("op".into(), "run".into(), "work".into(), "project".into(), root.clone(), 1,
            ApprovalMode::Open, &json!({"title":"synthetic edit", "mutations":[{"kind":"overwrite_text", "path":"credentials/data.txt", "content":"changed"}]}));
        assert!(result.is_err());
        assert_eq!(
            fs::read_to_string(root.join("credentials/data.txt")).unwrap(),
            "synthetic marker"
        );
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn file_search_completeness_and_oversize_budget() {
        let root = temp_dir();
        let recovery = temp_dir();
        fs::write(root.join("large.txt"), vec![b'a'; 1048577]).unwrap();
        let manager = AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let data: Value = serde_json::from_str(
            &manager
                .execute_read(
                    &root,
                    "fs.search",
                    &json!({"path":".", "query":"a", "mode":"content"}),
                )
                .unwrap(),
        )
        .unwrap();
        assert_eq!(data["matches"], json!([]));
        assert_eq!(data["complete"], false);
        assert_eq!(data["truncated"], true);
        assert_eq!(data["stop_reason"], "bytes");
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn file_search_rejects_numeric_cursor() {
        let root = temp_dir();
        let recovery = temp_dir();
        let manager = AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let result = manager.execute_read(
            &root,
            "fs.search",
            &json!({"path":".", "query":"a", "mode":"name", "cursor":"200"}),
        );
        assert!(result.is_err_and(|error| error.starts_with("invalid_search_cursor:")));
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn recursive_search_and_list_prune_sensitive_components() {
        let root = temp_dir();
        let recovery = temp_dir();
        for name in [
            ".env",
            "nested/.env.local",
            "nested/.git/config",
            "credentials/data.txt",
            "nested/secrets/data.txt",
            "normal.txt",
        ] {
            let path = root.join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, "synthetic marker").unwrap();
        }
        let manager = AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        for mode in ["content", "name"] {
            let response = manager.execute_read(&root, "fs.search", &json!({"path":".", "query":if mode == "name" { "." } else { "synthetic marker" }, "mode":mode}));
            let data: Value = serde_json::from_str(&response.unwrap()).unwrap();
            assert_eq!(data["matches"], json!(["normal.txt"]));
        }
        let listing: Value = serde_json::from_str(
            &manager
                .execute_read(&root, "fs.list", &json!({"path":"."}))
                .unwrap(),
        )
        .unwrap();
        assert_eq!(listing["entries"], json!(["nested/", "normal.txt"]));
        let normal = manager
            .execute_read(&root, "fs.read", &json!({"path":"normal.txt"}))
            .unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&normal).unwrap()["content"],
            "synthetic marker"
        );
        assert!(manager
            .execute_read(&root, "fs.read", &json!({"path":"credentials/data.txt"}))
            .is_err());
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    fn assert_planner_patch_risk(content: Option<Value>, lines: usize, risk: OperationRisk) {
        let root = temp_dir();
        let recovery = temp_dir();
        fs::write(root.join("note.txt"), "unique original text").unwrap();
        let replacement = std::iter::repeat_n("replacement", lines)
            .collect::<Vec<_>>()
            .join("\n");
        let mut mutation = json!({"kind":"patch_text","path":"note.txt","old_text":"unique original text","new_text":replacement});
        if let Some(content) = content {
            mutation["content"] = content;
        }
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let disposition = manager
            .prepare_changes(
                "op".into(),
                "run".into(),
                "work".into(),
                "project".into(),
                root.clone(),
                7,
                ApprovalMode::AiReview,
                &json!({"title":"patch risk", "mutations":[mutation.clone()]}),
            )
            .unwrap();
        let view = match disposition {
            ChangeDisposition::NeedsHuman(view) => {
                assert_eq!(risk, OperationRisk::High);
                view
            }
            ChangeDisposition::NeedsAiReview(view) => {
                assert_ne!(
                    risk,
                    OperationRisk::High,
                    "large canonical patch bypassed required human approval"
                );
                view
            }
            ChangeDisposition::Executed(_) => panic!("patch must require review"),
        };
        assert_eq!(view.risk, risk);
        let plan = view.plan.as_ref().unwrap();
        assert_eq!(plan.effective_risk, risk);
        assert_eq!(classify_mutations(&[mutation]), risk);
        assert_eq!(
            classify_mutations(&[serde_json::to_value(&plan.steps[0].mutation).unwrap()]),
            risk
        );
        assert_eq!(
            fs::read_to_string(root.join("note.txt")).unwrap(),
            "unique original text"
        );
        assert!(manager.pending_change_set(&view.id).is_some());
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn change_set_planner_executes_exact_case_and_unicode_names() {
        let root = temp_dir();
        let recovery = temp_dir();
        fs::write(root.join("İ.txt"), "original").unwrap();
        fs::write(root.join("i\u{0307}.txt"), "distinct").unwrap();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        for mutations in [
            json!([{"kind":"create_text","path":"README.md","content":"readme"}]),
            json!([{"kind":"patch_text","path":"İ.txt","old_text":"original","new_text":"patched"}]),
            json!([{"kind":"copy","source_path":"İ.txt","path":"Copy.txt"}]),
        ] {
            let disposition = manager
                .prepare_changes(
                    "op".into(),
                    "run".into(),
                    "work".into(),
                    "project".into(),
                    root.clone(),
                    7,
                    ApprovalMode::Human,
                    &json!({"title":"exact spelling", "mutations":mutations}),
                )
                .unwrap();
            if let ChangeDisposition::Executed(_) = disposition {
                continue;
            }
            let view = review_pending(disposition);
            manager
                .resolve_change_set(
                    &view.id,
                    true,
                    Some(&view.plan.as_ref().unwrap().approval()),
                    &root,
                    7,
                )
                .unwrap();
        }
        let names: Vec<_> = fs::read_dir(&root)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        assert!(names.contains(&"README.md".to_string()));
        assert!(names.contains(&"Copy.txt".to_string()));
        assert_eq!(fs::read_to_string(root.join("İ.txt")).unwrap(), "patched");
        assert_eq!(
            fs::read_to_string(root.join("Copy.txt")).unwrap(),
            "patched"
        );
        assert_eq!(
            fs::read_to_string(root.join("i\u{0307}.txt")).unwrap(),
            "distinct"
        );
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }
    #[test]
    fn change_set_planner_large_patch_without_content_requires_human() {
        assert_planner_patch_risk(None, 1_001, OperationRisk::High);
    }

    #[test]
    fn change_set_planner_large_patch_with_null_content_requires_human() {
        assert_planner_patch_risk(Some(Value::Null), 1_001, OperationRisk::High);
    }

    #[test]
    fn change_set_planner_patch_risk_uses_applied_text_and_preserves_threshold() {
        for content in [
            None,
            Some(Value::Null),
            Some(json!("")),
            Some(json!("irrelevant")),
        ] {
            assert_planner_patch_risk(content.clone(), 1_000, OperationRisk::Medium);
            assert_planner_patch_risk(content, 1_001, OperationRisk::High);
        }
        assert_planner_patch_risk(None, 1, OperationRisk::Medium);
    }

    #[test]
    fn change_set_planner_create_line_thresholds_preserve_routine_policy() {
        for (lines, risk) in [
            (1, OperationRisk::Routine),
            (200, OperationRisk::Routine),
            (201, OperationRisk::Medium),
            (1_000, OperationRisk::Medium),
            (1_001, OperationRisk::High),
        ] {
            let root = temp_dir();
            let recovery = temp_dir();
            let mut manager =
                AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
            let content = std::iter::repeat_n("created", lines)
                .collect::<Vec<_>>()
                .join("\n");
            let disposition = manager.prepare_changes("op".into(), "run".into(), "work".into(), "project".into(), root.clone(), 7, ApprovalMode::AiReview,
                &json!({"title":"create risk", "mutations":[{"kind":"create_text","path":"note.txt","content":content}]})).unwrap();
            let view = match disposition {
                ChangeDisposition::Executed(result) => {
                    assert_eq!(risk, OperationRisk::Routine);
                    assert_eq!(fs::read_to_string(root.join("note.txt")).unwrap(), content);
                    result.change_set.unwrap()
                }
                ChangeDisposition::NeedsAiReview(view) => {
                    assert_eq!(risk, OperationRisk::Medium);
                    assert!(!root.join("note.txt").exists());
                    view
                }
                ChangeDisposition::NeedsHuman(view) => {
                    assert_eq!(risk, OperationRisk::High);
                    assert!(!root.join("note.txt").exists());
                    view
                }
            };
            assert_eq!(view.risk, risk);
            assert_eq!(view.plan.unwrap().effective_risk, risk);
            fs::remove_dir_all(root).unwrap();
            fs::remove_dir_all(recovery).unwrap();
        }
    }

    fn planner_arguments() -> Value {
        json!({"title":"dependent change", "mutations":[
        {"kind":"create_text","path":"a/b/note.txt","content":"expected"}, {"kind":"mkdir","path":"a/b"}, {"kind":"mkdir","path":"a"}, {"kind":"create_text","path":"independent.txt","content":"independent"}]})
    }

    #[test]
    fn change_set_planner_approval_errors_preserve_pending_and_exact_subset_executes_once() {
        let root = temp_dir();
        let recovery = temp_dir();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let view = review_pending(
            manager
                .prepare_changes(
                    "op".into(),
                    "run".into(),
                    "work".into(),
                    "project".into(),
                    root.clone(),
                    7,
                    ApprovalMode::Human,
                    &planner_arguments(),
                )
                .unwrap(),
        );
        assert!(!root.join("a").exists());
        let plan = view.plan.as_ref().unwrap();
        let full = plan.approval();
        let child = plan
            .steps
            .iter()
            .find(|step| step.mutation.path == "a/b/note.txt")
            .unwrap();
        let mut invalids = vec![None];
        let mut forged = full.clone();
        forged.plan_id.push('x');
        invalids.push(Some(forged));
        let mut forged = full.clone();
        forged.plan_hash.push('x');
        invalids.push(Some(forged));
        let mut forged = full.clone();
        forged.approved_operation_ids.push("forged".into());
        invalids.push(Some(forged));
        let mut forged = full.clone();
        forged
            .approved_operation_ids
            .push(child.operation_id.clone());
        invalids.push(Some(forged));
        let mut missing = full.clone();
        missing.approved_operation_ids = vec![child.operation_id.clone()];
        invalids.push(Some(missing));
        for approval in invalids {
            assert!(manager
                .resolve_change_set(&view.id, true, approval.as_ref(), &root, 7)
                .is_err());
            assert!(manager.pending_change_set(&view.id).is_some());
            assert_eq!(fs::read_dir(&root).unwrap().count(), 0);
        }
        assert!(manager
            .resolve_change_set(&view.id, true, Some(&full), &root, 8)
            .unwrap_err()
            .starts_with("scope_changed:"));
        assert!(manager.pending_change_set(&view.id).is_some());
        let original_expiry = manager.pending[&view.id].expires_at;
        manager.pending.get_mut(&view.id).unwrap().expires_at =
            Instant::now() - Duration::from_secs(1);
        assert!(manager
            .resolve_change_set(&view.id, true, Some(&full), &root, 7)
            .unwrap_err()
            .starts_with("approval_expired:"));
        assert!(manager.pending_change_set(&view.id).is_some());
        manager.pending.get_mut(&view.id).unwrap().expires_at = original_expiry;
        manager.pending.get_mut(&view.id).unwrap().view.plan = None;
        assert!(manager
            .resolve_change_set(&view.id, true, Some(&full), &root, 7)
            .unwrap_err()
            .starts_with("approval_replan_required:"));
        assert!(manager.pending_change_set(&view.id).is_some());
        manager.pending.get_mut(&view.id).unwrap().view.plan = Some(plan.clone());
        let selection = PlanApproval {
            approved_operation_ids: std::iter::once(child.operation_id.clone())
                .chain(child.depends_on.clone())
                .collect(),
            ..full
        };
        let result = manager
            .resolve_change_set(&view.id, true, Some(&selection), &root, 7)
            .unwrap();
        let completed = result.change_set.unwrap();
        assert_eq!(completed.status, "completed");
        assert_eq!(completed.plan.as_ref().unwrap().plan_hash, plan.plan_hash);
        assert_eq!(completed.mutations.len(), 3);
        assert!(!root.join("independent.txt").exists());
        assert_eq!(
            fs::read_to_string(root.join("a/b/note.txt")).unwrap(),
            "expected"
        );
        let journal = manager.journal_store().read(&view.id).unwrap();
        assert_eq!(
            journal.plan_hash,
            plan.execution_hash(&completed.mutations).unwrap()
        );
        assert!(manager
            .resolve_change_set(&view.id, true, Some(&selection), &root, 7)
            .unwrap_err()
            .starts_with("approval_missing:"));
        manager.undo(&view.id, &root, 7).unwrap();
        assert!(!root.join("a").exists());
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn change_set_planner_external_changes_preserve_pending_for_rejection() {
        for mode in [ApprovalMode::Human, ApprovalMode::AiReview] {
            let root = temp_dir();
            let recovery = temp_dir();
            let mut manager =
                AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
            let view = review_pending(
                manager
                    .prepare_changes(
                        "op".into(),
                        "run".into(),
                        "work".into(),
                        "project".into(),
                        root.clone(),
                        7,
                        mode,
                        &planner_arguments(),
                    )
                    .unwrap(),
            );
            fs::create_dir(root.join("a")).unwrap();
            fs::write(root.join("a/external.txt"), "external").unwrap();
            assert!(manager
                .resolve_change_set(
                    &view.id,
                    true,
                    Some(&view.plan.as_ref().unwrap().approval()),
                    &root,
                    7
                )
                .is_err());
            assert!(manager.pending_change_set(&view.id).is_some());
            assert!(!root.join("a/b").exists());
            manager
                .resolve_change_set(&view.id, false, None, &root, 7)
                .unwrap();
            assert_eq!(
                fs::read_to_string(root.join("a/external.txt")).unwrap(),
                "external"
            );
            fs::remove_dir_all(root).unwrap();
            fs::remove_dir_all(recovery).unwrap();
        }
    }

    #[test]
    fn change_set_planner_faults_at_each_dependent_step_recover_without_forward_replay() {
        for stage in [
            "before_step",
            "after_intent",
            "after_write",
            "after_step_journal",
        ] {
            for index in 0..3 {
                let root = temp_dir();
                let recovery = temp_dir();
                let mut manager =
                    AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
                let view = review_pending(
                    manager
                        .prepare_changes(
                            "op".into(),
                            "run".into(),
                            "work".into(),
                            "project".into(),
                            root.clone(),
                            7,
                            ApprovalMode::Human,
                            &planner_arguments(),
                        )
                        .unwrap(),
                );
                let mut approval = view.plan.as_ref().unwrap().approval();
                approval.approved_operation_ids.retain(|id| {
                    view.plan
                        .as_ref()
                        .unwrap()
                        .steps
                        .iter()
                        .any(|s| s.operation_id == *id && s.mutation.path != "independent.txt")
                });
                crate::change_set_recovery::tests::fault(stage, index, false);
                assert!(
                    manager
                        .resolve_change_set(&view.id, true, Some(&approval), &root, 7)
                        .is_err(),
                    "{stage} {index}"
                );
                let expected = if stage == "after_write" {
                    "recovery_required"
                } else {
                    "rolled_back"
                };
                assert_eq!(
                    manager
                        .list_change_sets()
                        .iter()
                        .find(|v| v.id == view.id)
                        .unwrap()
                        .status,
                    expected,
                    "{stage} {index}"
                );
                drop(manager);
                for _ in 0..2 {
                    let mut restarted =
                        AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
                    let scope = restarted.recovery_scope(&root, 7, "project");
                    let outcome = restarted.recover_execution(&view.id, &scope).unwrap();
                    assert_eq!(outcome.status(), expected, "{stage} {index}");
                    assert!(!root.join("independent.txt").exists());
                }
                if expected == "rolled_back" {
                    assert!(!root.join("a").exists());
                }
                fs::remove_dir_all(root).unwrap();
                fs::remove_dir_all(recovery).unwrap();
            }
        }
    }

    #[test]
    fn change_set_planner_replaced_created_parent_never_redirects_child_write() {
        let root = temp_dir();
        let recovery = temp_dir();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let changed_root = root.clone();
        crate::change_set_recovery::tests::action("after_step_journal", move || {
            fs::rename(changed_root.join("a"), changed_root.join("external-backup")).unwrap();
            fs::create_dir(changed_root.join("a")).unwrap();
            fs::write(changed_root.join("a/external.txt"), "external").unwrap();
        });
        assert!(manager
            .prepare_changes(
                "op".into(),
                "run".into(),
                "work".into(),
                "project".into(),
                root.clone(),
                7,
                ApprovalMode::Open,
                &planner_arguments()
            )
            .is_err());
        let view = manager.list_change_sets().into_iter().next().unwrap();
        assert_eq!(view.status, "rollback_conflicted");
        assert_eq!(
            fs::read_to_string(root.join("a/external.txt")).unwrap(),
            "external"
        );
        assert!(!root.join("a/b").exists());
        assert!(root.join("external-backup").is_dir());
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn change_set_planner_dependent_crash_receipts_bind_authority_on_restart() {
        for stage in [
            "before_step",
            "after_intent",
            "after_write",
            "after_step_journal",
        ] {
            for index in 0..3 {
                let root = temp_dir();
                let recovery = temp_dir();
                let mut manager =
                    AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
                let view = review_pending(
                    manager
                        .prepare_changes(
                            "op".into(),
                            "run".into(),
                            "work".into(),
                            "project".into(),
                            root.clone(),
                            7,
                            ApprovalMode::Human,
                            &planner_arguments(),
                        )
                        .unwrap(),
                );
                let mut approval = view.plan.as_ref().unwrap().approval();
                approval.approved_operation_ids.retain(|id| {
                    view.plan
                        .as_ref()
                        .unwrap()
                        .steps
                        .iter()
                        .any(|s| s.operation_id == *id && s.mutation.path != "independent.txt")
                });
                crate::change_set_recovery::tests::fault(stage, index, true);
                assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(
                    || manager.resolve_change_set(&view.id, true, Some(&approval), &root, 7)
                ))
                .is_err());
                drop(manager);
                let mut restarted =
                    AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
                let mut scope = restarted.recovery_scope(&root, 8, "project");
                assert!(restarted
                    .recover_execution(&view.id, &scope)
                    .unwrap_err()
                    .starts_with("recovery_scope_mismatch:"));
                scope.root_generation = 7;
                let expected = if stage == "after_write" {
                    "recovery_required"
                } else {
                    "rolled_back"
                };
                for _ in 0..2 {
                    assert_eq!(
                        restarted
                            .recover_execution(&view.id, &scope)
                            .unwrap()
                            .status(),
                        expected,
                        "{stage} {index}"
                    );
                }
                assert!(!root.join("independent.txt").exists());
                if expected == "rolled_back" {
                    assert!(!root.join("a").exists());
                }
                fs::remove_dir_all(root).unwrap();
                fs::remove_dir_all(recovery).unwrap();
            }
        }
    }

    #[test]
    fn change_set_planner_external_child_blocks_directory_compensation() {
        let root = temp_dir();
        let recovery = temp_dir();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let changed_root = root.clone();
        crate::change_set_recovery::tests::action("after_step_journal", move || {
            fs::write(changed_root.join("a/external.txt"), "external").unwrap();
        });
        crate::change_set_recovery::tests::fault("before_step", 1, false);
        assert!(manager
            .prepare_changes(
                "op".into(),
                "run".into(),
                "work".into(),
                "project".into(),
                root.clone(),
                7,
                ApprovalMode::Open,
                &planner_arguments()
            )
            .is_err());
        let view = manager.list_change_sets().into_iter().next().unwrap();
        assert_eq!(view.status, "rollback_conflicted");
        assert_eq!(
            fs::read_to_string(root.join("a/external.txt")).unwrap(),
            "external"
        );
        assert!(!root.join("a/b").exists());
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn change_set_planner_preflight_observes_every_path_before_any_write() {
        use std::sync::{
            atomic::{AtomicBool, Ordering},
            Arc,
        };
        let root = temp_dir();
        let recovery = temp_dir();
        fs::write(root.join("z.txt"), "original").unwrap();
        let changed_root = root.clone();
        let wrote = Arc::new(AtomicBool::new(false));
        let observed = wrote.clone();
        crate::change_set_recovery::tests::action("snapshot_after", move || {
            fs::rename(
                changed_root.join("z.txt"),
                changed_root.join("external-backup.txt"),
            )
            .unwrap();
            fs::write(changed_root.join("z.txt"), "original").unwrap();
            crate::change_set_recovery::tests::action("after_step_journal", move || {
                observed.store(true, Ordering::SeqCst);
            });
        });
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let result = manager.prepare_changes("op".into(), "run".into(), "work".into(), "project".into(), root.clone(), 1, ApprovalMode::Open,
            &json!({"title":"preflight", "mutations":[{"kind":"mkdir","path":"a"}, {"kind":"overwrite_text","path":"z.txt","content":"changed"}]}));
        crate::change_set_recovery::tests::clear_hooks();
        assert!(result.is_err());
        assert!(
            !wrote.load(Ordering::SeqCst),
            "a scoped write occurred before every precondition passed"
        );
        assert_eq!(fs::read_to_string(root.join("z.txt")).unwrap(), "original");
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    fn mixed_case_parent_arguments() -> Value {
        json!({"title":"mixed case parents", "mutations":[
            {"kind":"create_text","path":"a/B/c/Note.txt","content":"expected"},
            {"kind":"mkdir","path":"A/b/C"},
            {"kind":"mkdir","path":"a/B"},
            {"kind":"mkdir","path":"A"}]})
    }

    #[test]
    fn mixed_case_parents_execute_exact_intent_and_undo_after_restart() {
        let root = temp_dir();
        let recovery = temp_dir();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let view = review_pending(
            manager
                .prepare_changes(
                    "op".into(),
                    "run".into(),
                    "work".into(),
                    "project".into(),
                    root.clone(),
                    7,
                    ApprovalMode::Human,
                    &mixed_case_parent_arguments(),
                )
                .unwrap(),
        );
        let plan = view.plan.as_ref().unwrap();
        assert_eq!(
            plan.steps
                .iter()
                .map(|s| s.mutation.path.as_str())
                .collect::<Vec<_>>(),
            ["A", "a/B", "A/b/C", "a/B/c/Note.txt"]
        );
        let result = manager.resolve_change_set(&view.id, true, Some(&plan.approval()), &root, 7);
        assert!(
            result.is_ok(),
            "accepted mixed-case graph must execute: {result:?}"
        );
        assert_eq!(
            fs::read_to_string(root.join("A/B/C/Note.txt")).unwrap(),
            "expected"
        );
        for (directory, expected_name) in
            [("", "A"), ("A", "B"), ("A/B", "C"), ("A/B/C", "Note.txt")]
        {
            let names: Vec<_> = fs::read_dir(root.join(directory))
                .unwrap()
                .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
                .collect();
            assert_eq!(names, [expected_name]);
        }
        drop(manager);
        let mut restarted =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        restarted.undo(&view.id, &root, 7).unwrap();
        assert!(!root.join("A").exists());
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn mixed_case_parents_compensate_receipts_and_preserve_external_children() {
        for scenario in ["rollback", "restart", "external_child"] {
            let root = temp_dir();
            let recovery = temp_dir();
            let mut manager =
                AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
            let view = review_pending(
                manager
                    .prepare_changes(
                        "op".into(),
                        "run".into(),
                        "work".into(),
                        "project".into(),
                        root.clone(),
                        7,
                        ApprovalMode::Human,
                        &mixed_case_parent_arguments(),
                    )
                    .unwrap(),
            );
            let approval = view.plan.as_ref().unwrap().approval();
            crate::change_set_recovery::tests::fault("before_step", 3, scenario != "rollback");
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                manager.resolve_change_set(&view.id, true, Some(&approval), &root, 7)
            }));
            crate::change_set_recovery::tests::clear_hooks();
            if scenario == "rollback" {
                assert!(result
                    .unwrap()
                    .unwrap_err()
                    .starts_with("injected_io_failure:"));
                assert!(!root.join("A").exists());
            } else {
                assert!(
                    result.is_err(),
                    "must reach injected crash after all parent receipts"
                );
                assert!(root.join("A/B/C").is_dir());
            }
            assert!(!root.join("A/B/C/Note.txt").exists());
            if scenario == "external_child" {
                fs::write(root.join("A/B/C/external.txt"), "preserve external").unwrap();
            }
            drop(manager);
            let mut restarted =
                AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
            let stale_scope = restarted.recovery_scope(&root, 8, "project");
            assert!(restarted
                .recover_execution(&view.id, &stale_scope)
                .unwrap_err()
                .starts_with("recovery_scope_mismatch:"));
            let scope = restarted.recovery_scope(&root, 7, "project");
            let expected = if scenario == "external_child" {
                "rollback_conflicted"
            } else {
                "rolled_back"
            };
            for _ in 0..2 {
                assert_eq!(
                    restarted
                        .recover_execution(&view.id, &scope)
                        .unwrap()
                        .status(),
                    expected
                );
                assert!(!root.join("A/B/C/Note.txt").exists());
            }
            if scenario == "external_child" {
                assert_eq!(
                    fs::read_to_string(root.join("A/B/C/external.txt")).unwrap(),
                    "preserve external"
                );
            } else {
                assert!(!root.join("A").exists());
            }
            fs::remove_dir_all(root).unwrap();
            fs::remove_dir_all(recovery).unwrap();
        }
    }

    #[test]
    fn change_set_planner_nested_create_executes_and_undoes() {
        let root = temp_dir();
        let recovery = temp_dir();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let result = manager.prepare_changes(
            "op".into(),
            "run".into(),
            "work".into(),
            "project".into(),
            root.clone(),
            1,
            ApprovalMode::Open,
            &json!({"title":"nested", "mutations":[
                {"kind":"create_text","path":"a/b/note.txt","content":"expected"},
                {"kind":"mkdir","path":"a/b"}, {"kind":"mkdir","path":"a"}]}),
        );
        assert!(
            result.is_ok(),
            "nested dependency plan must preflight: {:?}",
            result.err()
        );
        let ChangeDisposition::Executed(result) = result.unwrap() else {
            panic!("expected execution")
        };
        assert_eq!(
            fs::read_to_string(root.join("a/b/note.txt")).unwrap(),
            "expected"
        );
        manager
            .undo(&result.change_set.unwrap().id, &root, 1)
            .unwrap();
        assert!(!root.join("a").exists());
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn human_mode_runs_routine_create_and_can_undo() {
        let root = temp_dir();
        let recovery = temp_dir();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let result = manager.prepare_changes(
            "op".into(), "run".into(), "work".into(), "project".into(), root.clone(), 1,
            ApprovalMode::Human,
            &json!({"title":"Create note","mutations":[{"kind":"create_text","path":"note.txt","content":"hello"}]}),
        ).unwrap();
        let ChangeDisposition::Executed(result) = result else {
            panic!("expected execution")
        };
        assert!(result.change_set.as_ref().unwrap().mutations[0]
            .expected_after_hash
            .is_some());
        assert_eq!(fs::read_to_string(root.join("note.txt")).unwrap(), "hello");
        manager
            .undo(&result.change_set.as_ref().unwrap().id, &root, 1)
            .unwrap();
        assert!(!root.join("note.txt").exists());
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn human_mode_waits_for_delete_and_rechecks_scope() {
        let root = temp_dir();
        let recovery = temp_dir();
        fs::write(root.join("note.txt"), "hello").unwrap();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let pending = manager
            .prepare_changes(
                "op".into(),
                "run".into(),
                "work".into(),
                "project".into(),
                root.clone(),
                4,
                ApprovalMode::Human,
                &json!({"title":"Delete note","mutations":[{"kind":"delete","path":"note.txt"}]}),
            )
            .unwrap();
        let ChangeDisposition::NeedsHuman(view) = pending else {
            panic!("expected approval")
        };
        assert!(manager
            .resolve_change_set(
                &view.id,
                true,
                Some(&view.plan.as_ref().unwrap().approval()),
                &root,
                5
            )
            .is_err());
        assert!(root.join("note.txt").exists());
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn ai_review_mode_escalates_delete_to_visible_human_approval() {
        let root = temp_dir();
        let recovery = temp_dir();
        fs::create_dir(root.join("empty")).unwrap();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let pending = manager
            .prepare_changes(
                "op".into(),
                "run".into(),
                "work".into(),
                "project".into(),
                root.clone(),
                1,
                ApprovalMode::AiReview,
                &json!({"title":"Delete empty directory","mutations":[{"kind":"delete","path":"empty"}]}),
            )
            .unwrap();
        let ChangeDisposition::NeedsHuman(view) = pending else {
            panic!("high-risk delete must be handed to human approval")
        };
        assert_eq!(view.status, "waiting_user");
        assert_eq!(view.approval_mode, ApprovalMode::AiReview);
        assert!(manager.pending_change_set(&view.id).is_some());
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn disabled_executor_rejects_files_without_affecting_process_lifetime() {
        let root = temp_dir();
        let manager = AgentExecutionManager::disabled(root.join("recovery"), "test failure");
        let error = manager
            .execute_read(&root, "fs.list", &json!({"path":"."}))
            .unwrap_err();
        assert!(error.starts_with("tool_system_unavailable:"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn recursive_search_and_list_skip_protected_application_data() {
        let root = temp_dir();
        let app_data = root.join("app-data");
        let recovery = app_data.join("file-recovery");
        fs::create_dir_all(&recovery).unwrap();
        fs::write(app_data.join("secret.txt"), "do-not-expose").unwrap();
        fs::write(root.join("visible.txt"), "safe").unwrap();
        let manager = AgentExecutionManager::new(recovery, "test-secret").unwrap();
        let listing = manager
            .execute_read(&root, "fs.list", &json!({"path":"."}))
            .unwrap();
        assert!(!listing.contains("app-data"));
        let search = manager
            .execute_read(
                &root,
                "fs.search",
                &json!({"path":".","query":"do-not-expose","mode":"content"}),
            )
            .unwrap();
        assert!(!search.contains("secret.txt"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn approval_rejects_a_file_changed_after_the_proposal() {
        let root = temp_dir();
        let recovery = temp_dir();
        fs::write(root.join("note.txt"), "before").unwrap();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let proposal = manager
            .prepare_changes(
                "op".into(),
                "run".into(),
                "work".into(),
                "project".into(),
                root.clone(),
                1,
                ApprovalMode::Human,
                &json!({"title":"Update note","mutations":[{
                    "kind":"overwrite_text","path":"note.txt","content":"after"
                }]}),
            )
            .unwrap();
        let ChangeDisposition::NeedsHuman(view) = proposal else {
            panic!("expected approval")
        };
        fs::write(root.join("note.txt"), "external edit").unwrap();
        let error = manager
            .resolve_change_set(
                &view.id,
                true,
                Some(&view.plan.as_ref().unwrap().approval()),
                &root,
                1,
            )
            .unwrap_err();
        assert!(error.starts_with("file_changed_since_read:"));
        assert_eq!(
            fs::read_to_string(root.join("note.txt")).unwrap(),
            "external edit"
        );
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn partial_approval_executes_only_selected_precise_patch() {
        let root = temp_dir();
        let recovery = temp_dir();
        fs::write(root.join("one.txt"), "alpha").unwrap();
        fs::write(root.join("two.txt"), "beta").unwrap();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let proposal = manager
            .prepare_changes(
                "op".into(),
                "run".into(),
                "work".into(),
                "project".into(),
                root.clone(),
                1,
                ApprovalMode::Human,
                &json!({"title":"Patch notes","mutations":[
                    {"kind":"patch_text","path":"one.txt","old_text":"alpha","new_text":"changed-one"},
                    {"kind":"patch_text","path":"two.txt","old_text":"beta","new_text":"changed-two"}
                ]}),
            )
            .unwrap();
        let ChangeDisposition::NeedsHuman(view) = proposal else {
            panic!("expected approval")
        };
        manager
            .resolve_change_set(
                &view.id,
                true,
                Some(&PlanApproval {
                    approved_operation_ids: vec![view.plan.as_ref().unwrap().steps[1]
                        .operation_id
                        .clone()],
                    ..view.plan.as_ref().unwrap().approval()
                }),
                &root,
                1,
            )
            .unwrap();
        assert_eq!(fs::read_to_string(root.join("one.txt")).unwrap(), "alpha");
        assert_eq!(
            fs::read_to_string(root.join("two.txt")).unwrap(),
            "changed-two"
        );
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn undo_refuses_to_overwrite_a_later_external_edit() {
        let root = temp_dir();
        let recovery = temp_dir();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let disposition = manager
            .prepare_changes(
                "op".into(),
                "run".into(),
                "work".into(),
                "project".into(),
                root.clone(),
                1,
                ApprovalMode::Human,
                &json!({"title":"Create note","mutations":[{
                    "kind":"create_text","path":"note.txt","content":"agent edit"
                }]}),
            )
            .unwrap();
        let ChangeDisposition::Executed(result) = disposition else {
            panic!("expected execution")
        };
        fs::write(root.join("note.txt"), "later external edit").unwrap();
        let error = manager
            .undo(&result.change_set.as_ref().unwrap().id, &root, 1)
            .unwrap_err();
        assert!(error.starts_with("undo_conflict:"));
        assert_eq!(
            fs::read_to_string(root.join("note.txt")).unwrap(),
            "later external edit"
        );
        assert_eq!(
            manager
                .list_change_sets()
                .into_iter()
                .find(|item| item.id == result.change_set.as_ref().unwrap().id)
                .unwrap()
                .undo_state,
            "conflicted"
        );
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn project_undo_window_never_reactivates_the_eleventh_change() {
        let root = temp_dir();
        let recovery = temp_dir();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let mut change_ids = Vec::new();
        for index in 0..11 {
            let disposition = manager
                .prepare_changes(
                    format!("op-{index}"),
                    format!("run-{index}"),
                    format!("work-{}", index % 2),
                    "project".into(),
                    root.clone(),
                    1,
                    ApprovalMode::Human,
                    &json!({"title":format!("Create {index}"),"mutations":[{
                        "kind":"create_text","path":format!("note-{index}.txt"),"content":format!("{index}")
                    }]}),
                )
                .unwrap();
            let ChangeDisposition::Executed(result) = disposition else {
                panic!("expected execution")
            };
            change_ids.push(result.change_set.unwrap().id);
        }
        let oldest = manager
            .list_change_sets()
            .into_iter()
            .find(|item| item.undo_sequence == Some(1))
            .unwrap();
        assert_eq!(oldest.undo_state, "expired_depth_window");
        for index in (1..=10).rev() {
            manager.undo(&change_ids[index], &root, 1).unwrap();
        }
        let oldest = manager
            .list_change_sets()
            .into_iter()
            .find(|item| item.id == change_ids[0])
            .unwrap();
        assert_eq!(oldest.undo_state, "expired_depth_window");
        assert!(manager
            .undo(&change_ids[0], &root, 1)
            .unwrap_err()
            .starts_with("undo_depth_exceeded:"));
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn undo_catalog_survives_restart_and_pending_work_blocks_the_project() {
        let root = temp_dir();
        let recovery = temp_dir();
        let recovery_points = recovery.join("points");
        let change_id = {
            let mut manager =
                AgentExecutionManager::new(recovery_points.clone(), "test-secret").unwrap();
            let disposition = manager
                .prepare_changes(
                    "op".into(), "run".into(), "work-a".into(), "project".into(), root.clone(), 7,
                    ApprovalMode::Human,
                    &json!({"title":"Create note","mutations":[{"kind":"create_text","path":"note.txt","content":"hello"}]}),
                )
                .unwrap();
            let ChangeDisposition::Executed(result) = disposition else {
                panic!("expected execution")
            };
            result.change_set.unwrap().id
        };
        let mut restarted = AgentExecutionManager::new(recovery_points, "test-secret").unwrap();
        let restored = restarted
            .list_change_sets()
            .into_iter()
            .find(|item| item.id == change_id)
            .unwrap();
        assert_eq!(restored.undo_state, "available");
        let pending = restarted
            .prepare_changes(
                "op-2".into(),
                "run-2".into(),
                "work-b".into(),
                "project".into(),
                root.clone(),
                7,
                ApprovalMode::Human,
                &json!({"title":"Delete note","mutations":[{"kind":"delete","path":"note.txt"}]}),
            )
            .unwrap();
        assert!(matches!(pending, ChangeDisposition::NeedsHuman(_)));
        let blocked = restarted
            .list_change_sets()
            .into_iter()
            .find(|item| item.id == change_id)
            .unwrap();
        assert_eq!(blocked.undo_state, "blocked_by_pending_change");
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }

    #[test]
    fn undo_expires_at_the_twenty_four_hour_boundary() {
        let root = temp_dir();
        let recovery = temp_dir();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "test-secret").unwrap();
        let disposition = manager
            .prepare_changes(
                "op".into(), "run".into(), "work".into(), "project".into(), root.clone(), 1,
                ApprovalMode::Human,
                &json!({"title":"Create note","mutations":[{"kind":"create_text","path":"note.txt","content":"hello"}]}),
            )
            .unwrap();
        let ChangeDisposition::Executed(result) = disposition else {
            panic!("expected execution")
        };
        let recovery_id = result.recovery_point_id.unwrap();
        let change_id = result.change_set.unwrap().id;
        let now = manager.undo_catalog.last_seen_utc_ms;
        manager
            .undo_catalog
            .records
            .get_mut(&recovery_id)
            .unwrap()
            .created_at_epoch_ms = now - UNDO_WINDOW_MS;
        let view = manager
            .list_change_sets()
            .into_iter()
            .find(|item| item.id == change_id)
            .unwrap();
        assert_eq!(view.undo_state, "expired_time_window");
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }
    #[test]
    fn change_set_recovery_restart_surfaces_uncertain_execution_without_authorizing_or_replaying() {
        let root = temp_dir();
        let recovery = temp_dir();
        let points = recovery.join("points");
        let mut manager = AgentExecutionManager::new(points.clone(), "synthetic-key").unwrap();
        crate::change_set_recovery::tests::fault("after_write", 0, true);
        assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| manager.prepare_changes("op".into(),"run".into(),"work".into(),"project".into(),root.clone(),7,ApprovalMode::Open,&json!({"title":"synthetic","mutations":[{"kind":"create_text","path":"note.txt","content":"synthetic body"}]})))).is_err());
        drop(manager);
        for _ in 0..2 {
            let mut restarted =
                AgentExecutionManager::new(points.clone(), "synthetic-key").unwrap();
            let view = restarted.list_change_sets().into_iter().next().unwrap();
            assert_eq!(view.status, "recovery_required");
            assert_eq!(view.recovery_affected_paths, vec!["note.txt"]);
            assert_eq!(
                view.recovery_error_code.as_deref(),
                Some("recovery_required")
            );
            assert_ne!(view.undo_state, "available");
            assert_eq!(
                restarted
                    .recover_change_set(&view.id, &root, 7)
                    .unwrap()
                    .status,
                "recovery_required"
            );
            assert_eq!(
                fs::read_to_string(root.join("note.txt")).unwrap(),
                "synthetic body"
            );
            restarted.cleanup_recovery().unwrap();
            assert!(points
                .join(format!("{}.recovery", view.recovery_point_id.unwrap()))
                .exists());
        }
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }
    #[test]
    fn change_set_recovery_v1_decodes_read_only_and_never_forces_restore() {
        let root = temp_dir();
        let recovery = temp_dir();
        let mut manager =
            AgentExecutionManager::new(recovery.join("points"), "synthetic-key").unwrap();
        let id = Uuid::new_v4().to_string();
        let snapshot = RecoverySnapshot {
            id: id.clone(),
            project_id: "p".into(),
            conversation_id: "c".into(),
            created_at: "now".into(),
            entries: BTreeMap::from([(
                "a.txt".into(),
                SnapshotEntry {
                    kind: "file".into(),
                    content_base64: Some(BASE64.encode("before")),
                },
            )]),
            after: BTreeMap::new(),
        };
        write_encrypted(
            &manager.recovery_dir.join(format!("{id}.recovery")),
            &manager.key,
            &snapshot,
        )
        .unwrap();
        fs::write(root.join("a.txt"), "external").unwrap();
        let decoded = manager.read_snapshot(&id).unwrap();
        assert_eq!(decoded.id, id);
        let view: ChangeSetView = serde_json::from_value(json!({
            "id":"legacy-change", "operation_id":"op", "run_id":"run", "conversation_id":"c", "project_id":"p", "title":"legacy", "risk":"routine", "approval_mode":"human", "mutations":[], "status":"completed", "expires_at_epoch_ms":0, "recovery_point_id":id
        })).unwrap();
        let record = UndoRecord {
            change_set: view.clone(),
            root_generation: 1,
            created_at_epoch_ms: 0,
            undo_sequence: 1,
            status: "completed".into(),
        };
        assert!(manager
            .validate_undo_record(&record, 0)
            .unwrap_err()
            .starts_with("recovery_unverified:"));
        manager.undo_catalog.records.insert(id.clone(), record);
        let legacy_view = manager.decorate_undo_state(view, 0);
        assert_eq!(legacy_view.undo_state, "unavailable");
        assert_eq!(
            legacy_view.undo_block_reason.as_deref(),
            Some("recovery_unverified")
        );
        assert!(manager
            .restore_snapshot(&root, &decoded)
            .unwrap_err()
            .starts_with("recovery_unverified:"));
        assert_eq!(fs::read_to_string(root.join("a.txt")).unwrap(), "external");
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }
    fn review_prepare(
        manager: &mut AgentExecutionManager,
        root: &Path,
        mode: ApprovalMode,
        mutations: Value,
    ) -> Result<ChangeDisposition, String> {
        manager.prepare_changes(
            "synthetic-op".into(),
            "synthetic-run".into(),
            "synthetic-work".into(),
            "synthetic-project".into(),
            root.to_path_buf(),
            7,
            mode,
            &json!({"title":"synthetic review regression", "mutations":mutations}),
        )
    }
    fn review_pending(disposition: ChangeDisposition) -> ChangeSetView {
        match disposition {
            ChangeDisposition::NeedsHuman(view) | ChangeDisposition::NeedsAiReview(view) => view,
            _ => panic!("expected pending approval"),
        }
    }
    fn review_completed(disposition: ChangeDisposition) -> ChangeSetView {
        match disposition {
            ChangeDisposition::Executed(result) => result.change_set.unwrap(),
            _ => panic!("expected completed execution"),
        }
    }
    #[test]
    fn change_set_recovery_review_pending_approval_cannot_bypass_project_fence() {
        for conflict in [false, true] {
            for mode in [ApprovalMode::Human, ApprovalMode::AiReview] {
                let root = temp_dir();
                let recovery = temp_dir();
                let mut manager =
                    AgentExecutionManager::new(recovery.join("points"), "synthetic-key").unwrap();
                fs::write(root.join("a.txt"), "before a").unwrap();
                fs::write(root.join("b.txt"), "before b").unwrap();
                let first = review_pending(
                    review_prepare(
                        &mut manager,
                        &root,
                        mode,
                        json!([{"kind":"overwrite_text","path":"a.txt","content":"after a"}]),
                    )
                    .unwrap(),
                );
                let second = review_pending(
                    review_prepare(
                        &mut manager,
                        &root,
                        mode,
                        json!([{"kind":"overwrite_text","path":"b.txt","content":"after b"}]),
                    )
                    .unwrap(),
                );
                let stage = if conflict {
                    "after_step_journal"
                } else {
                    "write_flush"
                };
                if conflict {
                    let path = root.join("a.txt");
                    crate::change_set_recovery::tests::action(stage, move || {
                        fs::write(path, "external edit").unwrap()
                    });
                }
                crate::change_set_recovery::tests::fault(stage, 0, false);
                assert!(manager
                    .resolve_change_set(
                        &first.id,
                        true,
                        Some(&first.plan.as_ref().unwrap().approval()),
                        &root,
                        7
                    )
                    .is_err());
                let failed = manager
                    .list_change_sets()
                    .into_iter()
                    .find(|v| v.id == first.id)
                    .unwrap();
                assert_eq!(
                    failed.status,
                    if conflict {
                        "rollback_conflicted"
                    } else {
                        "recovery_required"
                    }
                );
                let error = manager
                    .resolve_change_set(
                        &second.id,
                        true,
                        Some(&second.plan.as_ref().unwrap().approval()),
                        &root,
                        7,
                    )
                    .unwrap_err();
                assert!(error.starts_with("recovery_required:"));
                assert!(
                    manager.pending_change_set(&second.id).is_some(),
                    "a blocked approval must remain reviewable"
                );
                assert_eq!(fs::read_to_string(root.join("b.txt")).unwrap(), "before b");
                manager.recent.clear(); // Isolate durable authority from the presentation cache.
                assert!(manager
                    .resolve_change_set(
                        &second.id,
                        true,
                        Some(&second.plan.as_ref().unwrap().approval()),
                        &root,
                        7
                    )
                    .unwrap_err()
                    .starts_with("recovery_required:"));
                assert!(manager.pending_change_set(&second.id).is_some());
                assert!(manager
                    .execute_view(&root, 7, second.clone())
                    .unwrap_err()
                    .starts_with("recovery_required:"));
                assert_eq!(fs::read_to_string(root.join("b.txt")).unwrap(), "before b");
                manager
                    .resolve_change_set(&second.id, false, None, &root, 7)
                    .unwrap();
                assert!(manager.pending_change_set(&second.id).is_none());
                fs::remove_dir_all(root).unwrap();
                fs::remove_dir_all(recovery).unwrap();
            }
        }
    }
    #[test]
    fn change_set_recovery_review_compensation_receipt_failure_fences_live_manager() {
        for explicit_recovery in [false, true] {
            let root = temp_dir();
            let recovery = temp_dir();
            let points = recovery.join("points");
            let mut manager = AgentExecutionManager::new(points.clone(), "synthetic-key").unwrap();
            let view=review_completed(review_prepare(&mut manager,&root,ApprovalMode::Open,json!([{"kind":"create_text","path":"a.txt","content":"a"},{"kind":"create_text","path":"b.txt","content":"b"}])).unwrap());
            // Freeze the observed clock so the injected storage failure targets the compensation receipt.
            manager.undo_catalog.last_seen_utc_ms = chrono::Utc::now().timestamp_millis() + 60_000;
            if explicit_recovery {
                crate::change_set_recovery::tests::fault("rollback_before", 0, false);
                let scope = manager.recovery_scope(&root, 7, "synthetic-project");
                assert!(manager
                    .journal_store()
                    .recover_execution(&view.id, &scope, true)
                    .is_err());
            }
            crate::change_set_recovery::tests::fault("storage_flush", 2, false);
            let result = if explicit_recovery {
                manager.recover_change_set(&view.id, &root, 7).map(|_| ())
            } else {
                manager.undo(&view.id, &root, 7).map(|_| ())
            };
            assert!(result.is_err());
            assert!(root.join("a.txt").exists());
            assert!(!root.join("b.txt").exists());
            let live = manager
                .list_change_sets()
                .into_iter()
                .find(|v| v.id == view.id)
                .unwrap();
            assert_eq!(live.status, "recovery_required");
            assert_ne!(live.undo_state, "available");
            assert_eq!(
                live.recovery_error_code.as_deref(),
                Some("recovery_required")
            );
            assert!(live.recovery_affected_paths.contains(&"b.txt".to_string()));
            assert!(manager.undo_target(&view.id).is_err());
            assert!(review_prepare(
                &mut manager,
                &root,
                ApprovalMode::Open,
                json!([{"kind":"create_text","path":"new.txt","content":"new"}])
            )
            .is_err());
            assert!(!root.join("new.txt").exists());
            drop(manager);
            for _ in 0..2 {
                let mut restarted =
                    AgentExecutionManager::new(points.clone(), "synthetic-key").unwrap();
                let pending = restarted
                    .list_change_sets()
                    .into_iter()
                    .find(|v| v.id == view.id)
                    .unwrap();
                assert_eq!(pending.status, "recovery_required");
                assert_ne!(pending.undo_state, "available");
            }
            let mut restarted = AgentExecutionManager::new(points, "synthetic-key").unwrap();
            assert_eq!(
                restarted
                    .recover_change_set(&view.id, &root, 7)
                    .unwrap()
                    .status,
                "rolled_back"
            );
            assert!(!root.join("a.txt").exists());
            fs::remove_dir_all(root).unwrap();
            fs::remove_dir_all(recovery).unwrap();
        }
    }
    #[test]
    fn change_set_recovery_review_terminal_journal_reconciles_undo_order_twice() {
        for crash_gap in [true, false] {
            let root = temp_dir();
            let recovery = temp_dir();
            let points = recovery.join("points");
            let mut manager = AgentExecutionManager::new(points.clone(), "synthetic-key").unwrap();
            let first = review_completed(
                review_prepare(
                    &mut manager,
                    &root,
                    ApprovalMode::Open,
                    json!([{"kind":"create_text","path":"a.txt","content":"a"}]),
                )
                .unwrap(),
            );
            let second = review_completed(
                review_prepare(
                    &mut manager,
                    &root,
                    ApprovalMode::Open,
                    json!([{"kind":"create_text","path":"b.txt","content":"b"}]),
                )
                .unwrap(),
            );
            let original =
                manager.undo_catalog.records[second.recovery_point_id.as_ref().unwrap()].clone();
            if crash_gap {
                let scope = manager.recovery_scope(&root, 7, "synthetic-project");
                assert_eq!(
                    manager
                        .journal_store()
                        .recover_execution(&second.id, &scope, true)
                        .unwrap()
                        .outcome(),
                    RecoveryOutcome::RolledBack
                );
                assert_eq!(
                    manager.undo_catalog.records[second.recovery_point_id.as_ref().unwrap()].status,
                    "completed"
                );
            } else {
                manager.undo(&second.id, &root, 7).unwrap();
            }
            drop(manager);
            for _ in 0..2 {
                let mut restarted =
                    AgentExecutionManager::new(points.clone(), "synthetic-key").unwrap();
                let views = restarted.list_change_sets();
                let older = views.iter().find(|v| v.id == first.id).unwrap();
                let newer = views.iter().find(|v| v.id == second.id).unwrap();
                assert_eq!(
                    newer.status,
                    if crash_gap { "rolled_back" } else { "undone" }
                );
                assert_eq!(older.undo_state, "available");
                assert!(restarted.undo_target(&first.id).is_ok());
                let record =
                    &restarted.undo_catalog.records[second.recovery_point_id.as_ref().unwrap()];
                assert_ne!(record.status, "completed");
                assert_eq!(record.undo_sequence, original.undo_sequence);
                assert_eq!(record.created_at_epoch_ms, original.created_at_epoch_ms);
                assert_eq!(record.root_generation, original.root_generation);
                assert_eq!(
                    record.change_set.undo_sequence,
                    original.change_set.undo_sequence
                );
                assert_eq!(record.change_set.created_at, original.change_set.created_at);
            }
            let mut restarted = AgentExecutionManager::new(points, "synthetic-key").unwrap();
            restarted.undo(&first.id, &root, 7).unwrap();
            assert!(!root.join("a.txt").exists());
            fs::remove_dir_all(root).unwrap();
            fs::remove_dir_all(recovery).unwrap();
        }
    }
    #[test]
    fn change_set_recovery_review_unreadable_journal_keeps_conservative_live_state() {
        let root = temp_dir();
        let recovery = temp_dir();
        let points = recovery.join("points");
        let mut manager = AgentExecutionManager::new(points.clone(), "synthetic-key").unwrap();
        let view=review_completed(review_prepare(&mut manager,&root,ApprovalMode::Open,json!([{"kind":"create_text","path":"a.txt","content":"a"},{"kind":"create_text","path":"b.txt","content":"b"}])).unwrap());
        let journal_path = points.join(format!("{}.journal", view.id));
        let backup = points.join("synthetic-preserved-journal");
        let replaced = journal_path.clone();
        let preserved = backup.clone();
        crate::change_set_recovery::tests::action("rollback_after_write", move || {
            fs::rename(&replaced, &preserved).unwrap();
            fs::create_dir(&replaced).unwrap();
        });
        assert!(manager.undo(&view.id, &root, 7).is_err());
        assert!(root.join("a.txt").exists());
        assert!(!root.join("b.txt").exists());
        let live = manager
            .list_change_sets()
            .into_iter()
            .find(|v| v.id == view.id)
            .unwrap();
        assert_eq!(live.status, "recovery_required");
        assert_eq!(live.undo_state, "unavailable");
        assert_eq!(live.recovery_affected_paths, vec!["a.txt", "b.txt"]);
        assert!(manager.undo_target(&view.id).is_err());
        assert!(review_prepare(
            &mut manager,
            &root,
            ApprovalMode::Open,
            json!([{"kind":"create_text","path":"new.txt","content":"new"}])
        )
        .is_err());
        assert!(!root.join("new.txt").exists());
        // Restore only the synthetic journal path, then reconcile through the normal manager API.
        fs::remove_dir(&journal_path).unwrap();
        fs::rename(backup, journal_path).unwrap();
        assert_eq!(
            manager
                .recover_change_set(&view.id, &root, 7)
                .unwrap()
                .status,
            "rolled_back"
        );
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }
    #[test]
    fn change_set_recovery_review_final_catalog_failure_preserves_terminal_undo_state() {
        let root = temp_dir();
        let recovery = temp_dir();
        let points = recovery.join("points");
        let mut manager = AgentExecutionManager::new(points.clone(), "synthetic-key").unwrap();
        let view=review_completed(review_prepare(&mut manager,&root,ApprovalMode::Open,json!([{"kind":"create_text","path":"a.txt","content":"a"},{"kind":"create_text","path":"b.txt","content":"b"}])).unwrap());
        manager.undo_catalog.last_seen_utc_ms = chrono::Utc::now().timestamp_millis() + 60_000;
        // rolling_back, two (intent,receipt) pairs, terminal journal, then catalog.
        crate::change_set_recovery::tests::fault("storage_flush", 6, false);
        assert!(manager.undo(&view.id, &root, 7).is_err());
        assert!(!root.join("a.txt").exists());
        assert!(!root.join("b.txt").exists());
        assert_eq!(
            manager
                .list_change_sets()
                .into_iter()
                .find(|v| v.id == view.id)
                .unwrap()
                .status,
            "undone"
        );
        drop(manager);
        for _ in 0..2 {
            let mut restarted =
                AgentExecutionManager::new(points.clone(), "synthetic-key").unwrap();
            let restored = restarted
                .list_change_sets()
                .into_iter()
                .find(|v| v.id == view.id)
                .unwrap();
            assert_eq!(restored.status, "undone");
            assert_eq!(restored.undo_state, "already_undone");
        }
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(recovery).unwrap();
    }
}
