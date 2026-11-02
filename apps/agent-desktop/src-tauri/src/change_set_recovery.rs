//! Durable, encrypted mutation receipts. A saved path is evidence, never authority.
use crate::{
    agent_execution::ChangeSetView,
    file_access::{windows_path_eq, AuthorizedRoot, FileAccessPolicy, FileIdentity},
};
use aes_gcm::{
    aead::{rand_core::RngCore, Aead, KeyInit, OsRng},
    Aes256Gcm, Nonce,
};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};
use uuid::Uuid;

const MAX_BYTES: u64 = 20 * 1024 * 1024;
const MAX_RECOVERY_BYTES: u64 = 2 * 1024 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryOutcome {
    Completed,
    RolledBack,
    RecoveryRequired,
    RollbackConflicted,
}
impl RecoveryOutcome {
    pub fn status(self) -> &'static str {
        match self {
            Self::Completed => "completed",
            Self::RolledBack => "rolled_back",
            Self::RecoveryRequired => "recovery_required",
            Self::RollbackConflicted => "rollback_conflicted",
        }
    }
    pub fn error_code(self) -> Option<&'static str> {
        match self {
            Self::RecoveryRequired => Some("recovery_required"),
            Self::RollbackConflicted => Some("rollback_incomplete"),
            _ => None,
        }
    }
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ObjectState {
    pub kind: String,
    pub identity: Option<FileIdentity>,
    pub sha256: Option<String>,
}
impl ObjectState {
    fn missing() -> Self {
        Self {
            kind: "missing".into(),
            identity: None,
            sha256: None,
        }
    }
    fn file(identity: FileIdentity, bytes: &[u8]) -> Self {
        Self {
            kind: "file".into(),
            identity: Some(identity),
            sha256: Some(hash(bytes)),
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SnapshotObject {
    pub state: ObjectState,
    pub content_base64: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecoverySnapshotV2 {
    pub snapshot_version: u32,
    pub id: String,
    pub execution_id: String,
    pub plan_hash: String,
    pub root_identity: FileIdentity,
    pub entries: BTreeMap<String, SnapshotObject>,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StepState {
    Pending,
    Intent,
    Applied,
    RollbackIntent,
    RolledBack,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MutationIntent {
    pub kind: String,
    pub path: String,
    pub source_path: Option<String>,
    pub planned_after_hash: Option<String>,
    // Reserved before I/O, including rollback staging. Interrupted artifacts are never guessed/deleted.
    pub artifact_id: String,
    pub rollback_artifact_id: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JournalStep {
    pub intent: MutationIntent,
    pub parents: BTreeMap<String, FileIdentity>,
    pub precondition: BTreeMap<String, ObjectState>,
    #[serde(default)]
    pub planned_parents: BTreeMap<String, String>,
    pub actual_identity: Option<FileIdentity>,
    pub after_hash: Option<String>,
    pub after: BTreeMap<String, ObjectState>,
    pub recovery_point_id: String,
    pub state: StepState,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecutionJournal {
    pub journal_version: u32,
    pub execution_id: String,
    pub plan_hash: String,
    pub root_generation: u64,
    pub root_identity: FileIdentity,
    pub root_path: PathBuf,
    pub state: String,
    pub steps: Vec<JournalStep>,
    pub view: ChangeSetView,
    pub affected_paths: Vec<String>,
}
impl ExecutionJournal {
    pub fn outcome(&self) -> RecoveryOutcome {
        match self.state.as_str() {
            "completed" => RecoveryOutcome::Completed,
            "rolled_back" => RecoveryOutcome::RolledBack,
            "rollback_conflicted" => RecoveryOutcome::RollbackConflicted,
            _ => RecoveryOutcome::RecoveryRequired,
        }
    }
    pub fn public_view(&self) -> ChangeSetView {
        let mut view = self.view.clone();
        view.execution_id = Some(self.execution_id.clone());
        view.recovery_point_id = self.steps.first().map(|s| s.recovery_point_id.clone());
        view.status = self.outcome().status().into();
        view.recovery_error_code = self.outcome().error_code().map(str::to_string);
        view.recovery_affected_paths = self.affected_paths.clone();
        if view.recovery_affected_paths.is_empty()
            && self.outcome() == RecoveryOutcome::RecoveryRequired
        {
            view.recovery_affected_paths = self
                .steps
                .iter()
                .flat_map(|s| s.precondition.keys().cloned())
                .collect();
            view.recovery_affected_paths.sort();
            view.recovery_affected_paths.dedup();
        }
        if self.state != "completed" {
            view.undo_state = "unavailable".into();
        }
        view
    }
}
// Forward bytes stay in memory; the encrypted journal contains no replayable action body.
pub struct Mutation {
    pub kind: String,
    pub path: String,
    pub source_path: Option<String>,
    pub bytes: Vec<u8>,
    pub expected_hash: Option<String>,
}
pub struct JournalStore {
    directory: PathBuf,
    key: [u8; 32],
}
impl JournalStore {
    pub fn new(directory: PathBuf, key: [u8; 32]) -> Self {
        Self { directory, key }
    }
    fn path(&self, id: &str, suffix: &str) -> Result<PathBuf, String> {
        Uuid::parse_str(id).map_err(|_| safe("recovery_invalid_id"))?;
        Ok(self.directory.join(format!("{id}.{suffix}")))
    }
    pub fn journals(&self) -> Result<Vec<ExecutionJournal>, String> {
        let mut result = Vec::new();
        for entry in fs::read_dir(&self.directory).map_err(|_| safe("recovery_unavailable"))? {
            let path = entry.map_err(|_| safe("recovery_unavailable"))?.path();
            if path.extension().and_then(|s| s.to_str()) == Some("journal") {
                let journal: ExecutionJournal = read_encrypted(&path, &self.key)?;
                if journal.journal_version != 1 {
                    return Err(safe("recovery_version_unsupported"));
                }
                result.push(journal);
            }
        }
        Ok(result)
    }
    pub fn read(&self, id: &str) -> Result<ExecutionJournal, String> {
        let journal: ExecutionJournal = read_encrypted(&self.path(id, "journal")?, &self.key)?;
        if journal.journal_version != 1 || journal.execution_id != id {
            return Err(safe("recovery_damaged"));
        }
        Ok(journal)
    }
    pub fn save(&self, journal: &ExecutionJournal) -> Result<(), String> {
        write_encrypted(
            &self.path(&journal.execution_id, "journal")?,
            &self.key,
            journal,
        )
    }
    pub fn prepare(
        &self,
        scope: &AuthorizedRoot,
        view: &ChangeSetView,
        mutations: &[Mutation],
    ) -> Result<ExecutionJournal, String> {
        let policy = FileAccessPolicy;
        let root = policy.open_read(scope, &policy.validate_relative(".")?)?;
        let plan_hash = if let Some(plan) = &view.plan {
            plan.validate(scope)?;
            plan.execution_hash(&view.mutations)?
        } else {
            hash(&serde_json::to_vec(&view.mutations).map_err(|_| safe("recovery_unavailable"))?)
        };
        let recovery_id = Uuid::new_v4().to_string();
        let mut snapshot = RecoverySnapshotV2 {
            snapshot_version: 2,
            id: recovery_id.clone(),
            execution_id: view.id.clone(),
            plan_hash: plan_hash.clone(),
            root_identity: root.identity(),
            entries: BTreeMap::new(),
        };
        let mut steps = Vec::new();
        let mut total = 0u64;
        for mutation in mutations {
            if mutation.bytes.len() as u64 > MAX_BYTES {
                return Err(safe("tool_limit_exceeded"));
            }
            let mut precondition = BTreeMap::new();
            let mut parents = BTreeMap::new();
            let mut planned_parents = BTreeMap::new();
            for path in std::iter::once(&mutation.path).chain(mutation.source_path.iter()) {
                match policy.open_parent(scope, &policy.validate_relative(path)?) {
                    Ok(parent) => {
                        parents.insert(path.clone(), parent.identity()?);
                    }
                    Err(e) if e.starts_with("path_not_found:") && view.plan.is_some() => {
                        let parent = path
                            .rsplit_once('/')
                            .map(|(parent, _)| parent)
                            .ok_or_else(|| safe("missing_parent_dependency"))?;
                        if !mutations
                            .iter()
                            .any(|m| m.kind == "mkdir" && windows_path_eq(&m.path, parent))
                        {
                            return Err(safe("missing_parent_dependency"));
                        }
                        planned_parents.insert(path.clone(), parent.to_string());
                    }
                    Err(e) => return Err(e),
                }
                let object = capture(scope, path)?;
                if let Some(plan) = &view.plan {
                    let approved = plan
                        .steps
                        .iter()
                        .find(|step| step.mutation.path == mutation.path)
                        .and_then(|step| step.preconditions.get(path))
                        .ok_or_else(|| safe("execution_plan_mismatch"))?;
                    if crate::change_set_planner::Precondition::from(object.state.clone())
                        != *approved
                    {
                        return Err(safe("file_changed_since_read"));
                    }
                }
                if let Some(bytes) = &object.content_base64 {
                    total += bytes.len() as u64;
                }
                if total > MAX_BYTES * 2 {
                    return Err(safe("tool_limit_exceeded"));
                }
                precondition.insert(path.clone(), object.state.clone());
                snapshot.entries.entry(path.clone()).or_insert(object);
            }
            let check_path = mutation.source_path.as_ref().unwrap_or(&mutation.path);
            if mutation.expected_hash.is_some()
                && precondition[check_path].sha256 != mutation.expected_hash
            {
                return Err(safe("file_changed_since_read"));
            }
            steps.push(JournalStep {
                intent: MutationIntent {
                    kind: mutation.kind.clone(),
                    path: mutation.path.clone(),
                    source_path: mutation.source_path.clone(),
                    planned_after_hash: if matches!(mutation.kind.as_str(), "mkdir" | "delete") {
                        None
                    } else if mutation.kind == "move" {
                        mutation.expected_hash.clone()
                    } else {
                        Some(hash(&mutation.bytes))
                    },
                    artifact_id: Uuid::new_v4().to_string(),
                    rollback_artifact_id: Uuid::new_v4().to_string(),
                },
                parents,
                planned_parents,
                precondition,
                actual_identity: None,
                after_hash: None,
                after: BTreeMap::new(),
                recovery_point_id: recovery_id.clone(),
                state: StepState::Pending,
            });
        }
        let mut view = view.clone();
        for mutation in &mut view.mutations {
            mutation.content = None;
            mutation.old_text = None;
            mutation.new_text = None;
        }
        if let Some(plan) = &mut view.plan {
            plan.redact();
        }
        let journal = ExecutionJournal {
            journal_version: 1,
            execution_id: view.id.clone(),
            plan_hash,
            root_generation: scope.root_generation,
            root_identity: root.identity(),
            root_path: scope.root.clone(),
            state: "prepared".into(),
            steps,
            view,
            affected_paths: Vec::new(),
        };
        let snapshot_bytes = serde_json::to_vec(&snapshot)
            .map_err(|_| safe("recovery_unavailable"))?
            .len() as u64;
        let used: u64 = fs::read_dir(&self.directory)
            .map_err(|_| safe("recovery_unavailable"))?
            .filter_map(Result::ok)
            .filter_map(|e| e.metadata().ok())
            .map(|m| m.len())
            .sum();
        if used
            .saturating_add(snapshot_bytes)
            .saturating_add(1024 * 1024)
            > MAX_RECOVERY_BYTES
        {
            return Err(safe("recovery_storage_full"));
        }
        checkpoint("snapshot_before")?;
        write_encrypted(&self.path(&recovery_id, "recovery")?, &self.key, &snapshot)?;
        checkpoint("snapshot_after")?;
        self.save(&journal)?;
        Ok(journal)
    }
    fn verify_scope(
        &self,
        journal: &ExecutionJournal,
        scope: &AuthorizedRoot,
    ) -> Result<crate::file_access::VerifiedReadHandle, String> {
        if journal.root_generation != scope.root_generation
            || journal.view.project_id != scope.project_id
            || journal.root_path != scope.root
        {
            return Err(safe("recovery_scope_mismatch"));
        }
        let root = FileAccessPolicy.open_read(scope, &FileAccessPolicy.validate_relative(".")?)?;
        if root.identity() != journal.root_identity {
            return Err(safe("recovery_scope_mismatch"));
        }
        Ok(root)
    }
    pub fn execute(
        &self,
        journal: &mut ExecutionJournal,
        scope: &AuthorizedRoot,
        mutations: &[Mutation],
    ) -> Result<(), String> {
        let _root = self.verify_scope(journal, scope)?;
        let durable = self.read(&journal.execution_id)?;
        if journal.state != "prepared"
            || durable.state != "prepared"
            || mutations.len() != journal.steps.len()
            || journal.steps.iter().any(|s| s.state != StepState::Pending)
        {
            return Err(safe("recovery_required"));
        }
        for (step, mutation) in journal.steps.iter().zip(mutations) {
            if step.intent.kind != mutation.kind
                || step.intent.path != mutation.path
                || step.intent.source_path != mutation.source_path
                || (!matches!(mutation.kind.as_str(), "move" | "mkdir" | "delete")
                    && step.intent.planned_after_hash.as_deref()
                        != Some(hash(&mutation.bytes).as_str()))
            {
                return Err(safe("execution_plan_mismatch"));
            }
        }
        if let Some(plan) = &journal.view.plan {
            plan.validate(scope)?;
        }
        for (index, mutation) in mutations.iter().enumerate() {
            checkpoint("before_step")?;
            let _root = self.verify_scope(journal, scope)?;
            for (path, parent_path) in journal.steps[index].planned_parents.clone() {
                let parent_step = journal.steps[..index]
                    .iter()
                    .find(|s| {
                        windows_path_eq(&s.intent.path, &parent_path)
                            && s.intent.kind == "mkdir"
                            && s.state == StepState::Applied
                    })
                    .ok_or_else(|| safe("missing_parent_dependency"))?;
                let identity = parent_step
                    .actual_identity
                    .ok_or_else(|| safe("recovery_unverified"))?;
                journal.steps[index].parents.insert(path, identity);
            }
            let _parents = pin_parents(scope, &journal.steps[index])?;
            let step = &mut journal.steps[index];
            if !matches_states(scope, &step.precondition)? {
                return Err(safe("file_changed_since_read"));
            }
            step.state = StepState::Intent;
            journal.state = "executing".into();
            self.save(journal)?;
            checkpoint("after_intent")?;
            let after = apply(scope, &journal.steps[index], mutation)?;
            checkpoint("after_write")?;
            let step = &mut journal.steps[index];
            step.actual_identity = after.get(&mutation.path).and_then(|s| s.identity);
            step.after_hash = after.get(&mutation.path).and_then(|s| s.sha256.clone());
            step.after = after;
            step.state = StepState::Applied;
            self.save(journal)?;
            checkpoint("after_step_journal")?;
        }
        journal.state = "completed".into();
        self.save(journal)?;
        checkpoint("after_completed")?;
        Ok(())
    }
    pub fn recover_execution(
        &self,
        id: &str,
        scope: &AuthorizedRoot,
        request_rollback: bool,
    ) -> Result<ExecutionJournal, String> {
        let mut journal = self.read(id)?;
        let _root = self.verify_scope(&journal, scope)?;
        if journal.state == "rolled_back" || (journal.state == "completed" && !request_rollback) {
            return Ok(journal);
        }
        let snapshot_id = journal
            .steps
            .first()
            .ok_or_else(|| safe("recovery_damaged"))?
            .recovery_point_id
            .clone();
        let snapshot: RecoverySnapshotV2 =
            read_encrypted(&self.path(&snapshot_id, "recovery")?, &self.key)?;
        if snapshot.snapshot_version != 2
            || snapshot.execution_id != journal.execution_id
            || snapshot.plan_hash != journal.plan_hash
            || snapshot.root_identity != journal.root_identity
        {
            return Err(safe("recovery_damaged"));
        }
        journal.affected_paths.clear();
        // Never replay a forward intent. Only an exact unchanged pre-state proves no compensation is needed.
        for step in &mut journal.steps {
            if step.state == StepState::Intent {
                if pin_parents(scope, step).is_ok()
                    && artifacts_absent(scope, step).unwrap_or(false)
                    && matches_states(scope, &step.precondition).unwrap_or(false)
                {
                    step.state = StepState::Pending;
                } else {
                    journal
                        .affected_paths
                        .extend(step.precondition.keys().cloned());
                }
            }
        }
        if !journal.affected_paths.is_empty() {
            journal.state = "recovery_required".into();
            journal.affected_paths.sort();
            journal.affected_paths.dedup();
            self.save(&journal)?;
            return Ok(journal);
        }
        journal.state = "rolling_back".into();
        self.save(&journal)?;
        let mut uncertain = false;
        for index in (0..journal.steps.len()).rev() {
            let step = &journal.steps[index];
            if matches!(step.state, StepState::Pending | StepState::RolledBack) {
                continue;
            }
            let _parents = match pin_parents(scope, step) {
                Ok(parents) => parents,
                Err(_) => {
                    uncertain |= step.state == StepState::RollbackIntent;
                    journal.affected_paths.extend(step.after.keys().cloned());
                    continue;
                }
            };
            if !artifacts_absent(scope, step).unwrap_or(false) {
                uncertain = true;
                journal.affected_paths.extend(step.after.keys().cloned());
                continue;
            }
            if step.state == StepState::RollbackIntent
                && rollback_already_done(scope, step, &snapshot).unwrap_or(false)
            {
                journal.steps[index].state = StepState::RolledBack;
                self.save(&journal)?;
                continue;
            }
            if !matches_states(scope, &step.after).unwrap_or(false) {
                uncertain |= step.state == StepState::RollbackIntent;
                journal.affected_paths.extend(step.after.keys().cloned());
                continue;
            }
            journal.steps[index].state = StepState::RollbackIntent;
            self.save(&journal)?;
            checkpoint("rollback_before")?;
            match compensate(scope, &journal.steps[index], &snapshot) {
                Ok(()) => {
                    checkpoint("rollback_after_write")?;
                    journal.steps[index].state = StepState::RolledBack;
                    self.save(&journal)?;
                    checkpoint("rollback_after_journal")?;
                }
                Err(_) => {
                    // Re-read on next attempt. A mutating call may have failed after changing disk.
                    journal
                        .affected_paths
                        .extend(journal.steps[index].after.keys().cloned());
                    uncertain |=
                        !matches_states(scope, &journal.steps[index].after).unwrap_or(false);
                }
            }
        }
        journal.affected_paths.sort();
        journal.affected_paths.dedup();
        journal.state = if journal.affected_paths.is_empty() {
            "rolled_back"
        } else if uncertain {
            "recovery_required"
        } else {
            "rollback_conflicted"
        }
        .into();
        self.save(&journal)?;
        Ok(journal)
    }
}
pub(crate) fn capture(scope: &AuthorizedRoot, path: &str) -> Result<SnapshotObject, String> {
    let policy = FileAccessPolicy;
    let relative = policy.validate_relative(path)?;
    match policy.open_read(scope, &relative) {
        Ok(file) if file.is_dir() => Ok(SnapshotObject {
            state: ObjectState {
                kind: "directory".into(),
                identity: Some(file.identity()),
                sha256: None,
            },
            content_base64: None,
        }),
        Ok(file) => {
            let bytes = file.read_bytes(MAX_BYTES)?;
            Ok(SnapshotObject {
                state: ObjectState::file(file.identity(), &bytes),
                content_base64: Some(BASE64.encode(bytes)),
            })
        }
        Err(e) if e.starts_with("path_not_found:") => {
            policy.validate_planned_target(scope, &relative)?;
            Ok(SnapshotObject {
                state: ObjectState::missing(),
                content_base64: None,
            })
        }
        Err(e) => Err(e),
    }
}
fn matches_states(
    scope: &AuthorizedRoot,
    states: &BTreeMap<String, ObjectState>,
) -> Result<bool, String> {
    for (path, state) in states {
        if capture(scope, path)?.state != *state {
            return Ok(false);
        }
    }
    Ok(true)
}
fn artifacts_absent(scope: &AuthorizedRoot, step: &JournalStep) -> Result<bool, String> {
    let target = FileAccessPolicy.validate_relative(&step.intent.path)?;
    let parent = target.as_path().parent().unwrap_or(Path::new(""));
    for id in [&step.intent.artifact_id, &step.intent.rollback_artifact_id] {
        for suffix in ["tmp", "bak"] {
            let path = parent.join(format!(".agent-{id}.{suffix}"));
            if capture(scope, &path.to_string_lossy())?.state.kind != "missing" {
                return Ok(false);
            }
        }
    }
    Ok(true)
}
fn pin_parents(
    scope: &AuthorizedRoot,
    step: &JournalStep,
) -> Result<Vec<crate::file_access::VerifiedParentHandle>, String> {
    step.parents
        .iter()
        .map(|(path, identity)| {
            let parent =
                FileAccessPolicy.open_parent(scope, &FileAccessPolicy.validate_relative(path)?)?;
            if parent.identity()? != *identity {
                return Err(safe("file_identity_changed"));
            }
            Ok(parent)
        })
        .collect()
}
fn apply(
    scope: &AuthorizedRoot,
    step: &JournalStep,
    mutation: &Mutation,
) -> Result<BTreeMap<String, ObjectState>, String> {
    let policy = FileAccessPolicy;
    let target = policy.validate_relative(&mutation.path)?;
    let before = &step.precondition[&mutation.path];
    let after = match mutation.kind.as_str() {
        "mkdir" => ObjectState {
            kind: "directory".into(),
            identity: Some(policy.create_directory_checked(scope, &target)?),
            sha256: None,
        },
        "delete" => {
            policy.delete_checked(scope, &target, before.sha256.as_deref(), before.identity)?;
            ObjectState::missing()
        }
        "move" => {
            let source = mutation
                .source_path
                .as_ref()
                .ok_or_else(|| safe("invalid_tool_call"))?;
            let state = &step.precondition[source];
            let identity = policy.move_file_checked(
                scope,
                &policy.validate_relative(source)?,
                &target,
                state.sha256.as_deref(),
                state.identity,
            )?;
            ObjectState {
                kind: "file".into(),
                identity: Some(identity),
                sha256: state.sha256.clone(),
            }
        }
        _ => {
            let identity = policy.write_checked(
                scope,
                &target,
                &mutation.bytes,
                before.kind == "missing",
                before.sha256.as_deref(),
                before.identity,
                &step.intent.artifact_id,
            )?;
            ObjectState::file(identity, &mutation.bytes)
        }
    };
    // Flush failure means uncertainty even if the replacement was visible; don't manufacture a receipt.
    checkpoint("write_flush")?;
    let mut states = BTreeMap::from([(mutation.path.clone(), after)]);
    if mutation.kind == "move" {
        states.insert(
            mutation.source_path.clone().unwrap(),
            ObjectState::missing(),
        );
    }
    Ok(states)
}
fn rollback_already_done(
    scope: &AuthorizedRoot,
    step: &JournalStep,
    snapshot: &RecoverySnapshotV2,
) -> Result<bool, String> {
    // Deleted newly-created objects and moved-back objects have provable terminal states.
    // A restored overwrite has a new identity, so a crash before its receipt remains uncertain.
    if !artifacts_absent(scope, step)? {
        return Ok(false);
    }
    for path in step.after.keys() {
        if capture(scope, path)?.state != snapshot.entries[path].state {
            return Ok(false);
        }
    }
    Ok(true)
}
fn compensate(
    scope: &AuthorizedRoot,
    step: &JournalStep,
    snapshot: &RecoverySnapshotV2,
) -> Result<(), String> {
    let policy = FileAccessPolicy;
    let path = policy.validate_relative(&step.intent.path)?;
    let before = &snapshot.entries[&step.intent.path];
    let after = &step.after[&step.intent.path];
    if step.intent.kind == "move" {
        let source = step
            .intent
            .source_path
            .as_ref()
            .ok_or_else(|| safe("recovery_damaged"))?;
        policy.move_file_checked(
            scope,
            &path,
            &policy.validate_relative(source)?,
            after.sha256.as_deref(),
            after.identity,
        )?;
        return Ok(());
    }
    match before.state.kind.as_str() {
        "missing" => policy.delete_checked(scope, &path, after.sha256.as_deref(), after.identity),
        "directory" if after.kind == "missing" => {
            policy.create_directory_checked(scope, &path).map(|_| ())
        }
        "file" => {
            let bytes = BASE64
                .decode(
                    before
                        .content_base64
                        .as_deref()
                        .ok_or_else(|| safe("recovery_damaged"))?,
                )
                .map_err(|_| safe("recovery_damaged"))?;
            policy
                .write_checked(
                    scope,
                    &path,
                    &bytes,
                    after.kind == "missing",
                    after.sha256.as_deref(),
                    after.identity,
                    &step.intent.rollback_artifact_id,
                )
                .map(|_| ())
        }
        _ => Err(safe("recovery_damaged")),
    }
}
fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn safe(code: &str) -> String {
    format!("{code}: File recovery requires verified local state")
}
pub fn write_encrypted<T: Serialize>(
    destination: &Path,
    key: &[u8; 32],
    value: &T,
) -> Result<(), String> {
    let plaintext = serde_json::to_vec(value).map_err(|_| safe("recovery_unavailable"))?;
    let cipher = Aes256Gcm::new_from_slice(key).map_err(|_| safe("recovery_unavailable"))?;
    let mut nonce = [0u8; 12];
    OsRng.fill_bytes(&mut nonce);
    let encrypted = cipher
        .encrypt(Nonce::from_slice(&nonce), plaintext.as_ref())
        .map_err(|_| safe("recovery_unavailable"))?;
    let temporary = destination.with_extension(format!("{}.tmp", Uuid::new_v4()));
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .map_err(|_| safe("recovery_unavailable"))?;
        file.write_all(&nonce)
            .and_then(|_| file.write_all(&encrypted))
            .map_err(|_| safe("recovery_unavailable"))?;
        checkpoint("storage_flush")?;
        file.sync_all().map_err(|_| safe("recovery_unavailable"))?;
        drop(file);
        atomic_replace(&temporary, destination)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}
fn read_encrypted<T: DeserializeOwned>(path: &Path, key: &[u8; 32]) -> Result<T, String> {
    let bytes = fs::read(path).map_err(|_| safe("recovery_unavailable"))?;
    if bytes.len() < 13 {
        return Err(safe("recovery_damaged"));
    }
    let cipher = Aes256Gcm::new_from_slice(key).map_err(|_| safe("recovery_unavailable"))?;
    let plaintext = cipher
        .decrypt(Nonce::from_slice(&bytes[..12]), &bytes[12..])
        .map_err(|_| safe("recovery_damaged"))?;
    serde_json::from_slice(&plaintext).map_err(|_| safe("recovery_damaged"))
}
#[cfg(windows)]
fn atomic_replace(from: &Path, to: &Path) -> Result<(), String> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };
    let from: Vec<u16> = from.as_os_str().encode_wide().chain(Some(0)).collect();
    let to: Vec<u16> = to.as_os_str().encode_wide().chain(Some(0)).collect();
    // Same-directory replacement, flushed payload, and Windows write-through namespace update.
    if unsafe {
        MoveFileExW(
            from.as_ptr(),
            to.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    } == 0
    {
        return Err(safe("recovery_unavailable"));
    }
    Ok(())
}
#[cfg(not(windows))]
fn atomic_replace(_: &Path, _: &Path) -> Result<(), String> {
    Err(safe("file_access_platform_unsupported"))
}
#[cfg(not(test))]
fn checkpoint(_: &str) -> Result<(), String> {
    Ok(())
}
#[cfg(test)]
fn checkpoint(stage: &str) -> Result<(), String> {
    tests::checkpoint(stage)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    thread_local! { static FAULT: std::cell::RefCell<Option<(&'static str, usize, bool)>> = const { std::cell::RefCell::new(None) }; }
    struct Action {
        stage: &'static str,
        run: Box<dyn FnOnce()>,
    }
    thread_local! { static ACTION: std::cell::RefCell<Option<Action>> = const { std::cell::RefCell::new(None) }; }
    pub(crate) fn action(stage: &'static str, run: impl FnOnce() + 'static) {
        ACTION.with(|cell| {
            *cell.borrow_mut() = Some(Action {
                stage,
                run: Box::new(run),
            })
        });
    }
    pub(crate) fn checkpoint(stage: &str) -> Result<(), String> {
        let action = ACTION.with(|cell| {
            let mut action = cell.borrow_mut();
            if action.as_ref().is_some_and(|action| action.stage == stage) {
                action.take()
            } else {
                None
            }
        });
        if let Some(action) = action {
            (action.run)();
        }
        let fire = FAULT.with(|cell| {
            let mut value = cell.borrow_mut();
            if let Some((name, count, crash)) = value.as_mut() {
                if *name == stage {
                    if *count == 0 {
                        let crash = *crash;
                        *value = None;
                        return Some(crash);
                    }
                    *count -= 1;
                }
            }
            None
        });
        match fire {
            Some(true) => panic!("synthetic crash at {stage}"),
            Some(false) => Err(safe("injected_io_failure")),
            None => Ok(()),
        }
    }
    pub(crate) fn clear_hooks() {
        ACTION.with(|cell| *cell.borrow_mut() = None);
        FAULT.with(|cell| *cell.borrow_mut() = None);
    }
    pub(crate) fn fault(stage: &'static str, count: usize, crash: bool) {
        FAULT.with(|cell| *cell.borrow_mut() = Some((stage, count, crash)));
    }
    struct Fixture {
        base: PathBuf,
        scope: AuthorizedRoot,
        store: JournalStore,
    }
    impl Fixture {
        fn new() -> Self {
            let base = std::env::temp_dir().join(format!("agent-h3-{}", Uuid::new_v4()));
            let root = base.join("project");
            let recovery = base.join("state");
            fs::create_dir_all(&root).unwrap();
            fs::create_dir(&recovery).unwrap();
            let base = base.canonicalize().unwrap();
            let root = root.canonicalize().unwrap();
            let recovery = recovery.canonicalize().unwrap();
            let mut scope = AuthorizedRoot::for_host_path(&root, &recovery);
            scope.project_id = "synthetic-project".into();
            scope.root_generation = 7;
            Self {
                base,
                scope,
                store: JournalStore::new(recovery, [42; 32]),
            }
        }
        fn prepare(&self, mutations: &[Mutation]) -> ExecutionJournal {
            let view: ChangeSetView = serde_json::from_value(serde_json::json!({
                "id": Uuid::new_v4().to_string(), "operation_id":"synthetic-operation", "run_id":"synthetic-run", "conversation_id":"synthetic-work", "project_id":"synthetic-project",
                "title":"synthetic recovery test", "risk":"routine", "approval_mode":"open", "mutations":[], "status":"ready", "expires_at_epoch_ms":0, "recovery_point_id":null
            })).unwrap();
            self.store.prepare(&self.scope, &view, mutations).unwrap()
        }
        fn restarted(&self) -> JournalStore {
            JournalStore::new(self.base.join("state"), [42; 32])
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            FAULT.with(|cell| *cell.borrow_mut() = None);
            fs::remove_dir_all(&self.base).unwrap();
        }
    }
    fn create(path: &str) -> Mutation {
        Mutation {
            kind: "create_text".into(),
            path: path.into(),
            source_path: None,
            bytes: b"synthetic agent content".to_vec(),
            expected_hash: None,
        }
    }
    fn crash_execute(
        f: &Fixture,
        journal: &mut ExecutionJournal,
        mutations: &[Mutation],
        stage: &'static str,
        count: usize,
    ) {
        fault(stage, count, true);
        assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| f
            .store
            .execute(journal, &f.scope, mutations)))
        .is_err());
    }
    #[test]
    fn change_set_recovery_crash_boundaries_never_replay_forward_writes() {
        for (stage, expected) in [
            ("before_step", RecoveryOutcome::RolledBack),
            ("after_intent", RecoveryOutcome::RolledBack),
            ("after_write", RecoveryOutcome::RecoveryRequired),
            ("after_step_journal", RecoveryOutcome::RolledBack),
            ("after_completed", RecoveryOutcome::Completed),
        ] {
            for step in 0..2 {
                if stage == "after_completed" && step > 0 {
                    continue;
                }
                let f = Fixture::new();
                let mutations = [create("a.txt"), create("b.txt")];
                let mut journal = f.prepare(&mutations);
                crash_execute(&f, &mut journal, &mutations, stage, step);
                let restarted = f.restarted();
                let first = restarted
                    .recover_execution(&journal.execution_id, &f.scope, false)
                    .unwrap();
                assert_eq!(first.outcome(), expected, "{stage} step {step}");
                let before = fs::read_dir(&f.scope.root).unwrap().count();
                let second = f
                    .restarted()
                    .recover_execution(&journal.execution_id, &f.scope, false)
                    .unwrap();
                assert_eq!(second.outcome(), expected);
                assert_eq!(fs::read_dir(&f.scope.root).unwrap().count(), before);
                if expected == RecoveryOutcome::RolledBack {
                    assert_eq!(before, 0);
                }
                if expected == RecoveryOutcome::RecoveryRequired {
                    assert!(!f.scope.root.join("b.txt").exists() || step == 1);
                }
            }
        }
    }
    #[test]
    fn change_set_recovery_preserves_external_child_and_reports_stable_conflict_twice() {
        let f = Fixture::new();
        let mutations = [Mutation {
            kind: "mkdir".into(),
            path: "created".into(),
            source_path: None,
            bytes: vec![],
            expected_hash: None,
        }];
        let mut journal = f.prepare(&mutations);
        f.store.execute(&mut journal, &f.scope, &mutations).unwrap();
        fs::write(
            f.scope.root.join("created/external.txt"),
            "original external child",
        )
        .unwrap();
        let first = f
            .store
            .recover_execution(&journal.execution_id, &f.scope, true)
            .unwrap();
        assert_eq!(first.outcome(), RecoveryOutcome::RollbackConflicted);
        assert_eq!(
            first.public_view().recovery_error_code.as_deref(),
            Some("rollback_incomplete")
        );
        assert_eq!(first.affected_paths, vec!["created"]);
        let second = f
            .restarted()
            .recover_execution(&journal.execution_id, &f.scope, false)
            .unwrap();
        assert_eq!(second.outcome(), RecoveryOutcome::RollbackConflicted);
        assert_eq!(
            fs::read_to_string(f.scope.root.join("created/external.txt")).unwrap(),
            "original external child"
        );
    }
    #[test]
    fn change_set_recovery_preserves_external_file_edit_and_identity_replacement() {
        for same_content in [false, true] {
            let f = Fixture::new();
            let mutations = [create("a.txt")];
            let mut journal = f.prepare(&mutations);
            f.store.execute(&mut journal, &f.scope, &mutations).unwrap();
            if same_content {
                fs::rename(
                    f.scope.root.join("a.txt"),
                    f.scope.root.join("original.txt"),
                )
                .unwrap();
                fs::write(f.scope.root.join("a.txt"), &mutations[0].bytes).unwrap();
            } else {
                fs::write(f.scope.root.join("a.txt"), "external edit").unwrap();
            }
            let bytes = fs::read(f.scope.root.join("a.txt")).unwrap();
            let restored = f
                .store
                .recover_execution(&journal.execution_id, &f.scope, true)
                .unwrap();
            assert_eq!(restored.outcome(), RecoveryOutcome::RollbackConflicted);
            assert_eq!(fs::read(f.scope.root.join("a.txt")).unwrap(), bytes);
        }
    }
    #[test]
    fn change_set_recovery_snapshot_and_intent_flush_fail_before_external_write() {
        for stage in ["snapshot_before", "storage_flush"] {
            let f = Fixture::new();
            let mutations = [create("a.txt")];
            fault(stage, 0, true);
            assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(
                || f.prepare(&mutations)
            ))
            .is_err());
            assert!(!f.scope.root.join("a.txt").exists());
        }
        let f = Fixture::new();
        let mutations = [create("a.txt")];
        let mut journal = f.prepare(&mutations);
        fault("storage_flush", 0, false);
        assert!(f.store.execute(&mut journal, &f.scope, &mutations).is_err());
        assert!(!f.scope.root.join("a.txt").exists());
        assert_eq!(
            f.restarted()
                .recover_execution(&journal.execution_id, &f.scope, false)
                .unwrap()
                .outcome(),
            RecoveryOutcome::RolledBack
        );
    }
    #[test]
    fn change_set_recovery_successful_write_failed_flush_is_uncertain_and_not_replayed() {
        let f = Fixture::new();
        let mutations = [create("a.txt")];
        let mut journal = f.prepare(&mutations);
        fault("write_flush", 0, false);
        assert!(f.store.execute(&mut journal, &f.scope, &mutations).is_err());
        let identity = capture(&f.scope, "a.txt").unwrap().state.identity;
        for _ in 0..2 {
            let recovered = f
                .restarted()
                .recover_execution(&journal.execution_id, &f.scope, false)
                .unwrap();
            assert_eq!(recovered.outcome(), RecoveryOutcome::RecoveryRequired);
            assert_eq!(capture(&f.scope, "a.txt").unwrap().state.identity, identity);
        }
    }
    #[test]
    fn change_set_recovery_interrupted_rollback_resumes_in_reverse_completed_order() {
        for stage in [
            "rollback_before",
            "rollback_after_write",
            "rollback_after_journal",
        ] {
            let f = Fixture::new();
            let mutations = [create("z.txt"), create("a.txt")];
            let mut journal = f.prepare(&mutations);
            f.store.execute(&mut journal, &f.scope, &mutations).unwrap();
            fault(stage, 0, true);
            assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| f
                .store
                .recover_execution(&journal.execution_id, &f.scope, true)))
            .is_err());
            if stage != "rollback_before" {
                assert!(!f.scope.root.join("a.txt").exists());
                assert!(f.scope.root.join("z.txt").exists());
            }
            for _ in 0..2 {
                assert_eq!(
                    f.restarted()
                        .recover_execution(&journal.execution_id, &f.scope, false)
                        .unwrap()
                        .outcome(),
                    RecoveryOutcome::RolledBack
                );
            }
        }
    }
    #[test]
    fn change_set_recovery_interrupted_overwrite_compensation_remains_uncertain() {
        let f = Fixture::new();
        fs::write(f.scope.root.join("a.txt"), "original").unwrap();
        let mut mutation = create("a.txt");
        mutation.kind = "overwrite_text".into();
        mutation.expected_hash = Some(hash(b"original"));
        let mutations = [mutation];
        let mut journal = f.prepare(&mutations);
        f.store.execute(&mut journal, &f.scope, &mutations).unwrap();
        fault("rollback_after_write", 0, true);
        assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| f
            .store
            .recover_execution(&journal.execution_id, &f.scope, true)))
        .is_err());
        for _ in 0..2 {
            assert_eq!(
                f.restarted()
                    .recover_execution(&journal.execution_id, &f.scope, false)
                    .unwrap()
                    .outcome(),
                RecoveryOutcome::RecoveryRequired
            );
        }
        assert_eq!(
            fs::read_to_string(f.scope.root.join("a.txt")).unwrap(),
            "original"
        );
    }
    #[test]
    fn change_set_recovery_rejects_changed_authority_and_root_identity() {
        let f = Fixture::new();
        let mutations = [create("a.txt")];
        let journal = f.prepare(&mutations);
        let mut scope = f.scope.clone();
        scope.root_generation += 1;
        assert!(f
            .store
            .recover_execution(&journal.execution_id, &scope, false)
            .unwrap_err()
            .starts_with("recovery_scope_mismatch:"));
        fs::rename(&f.scope.root, f.base.join("old-project")).unwrap();
        fs::create_dir(&f.scope.root).unwrap();
        assert!(f
            .store
            .recover_execution(&journal.execution_id, &f.scope, false)
            .unwrap_err()
            .starts_with("recovery_scope_mismatch:"));
    }
    #[test]
    fn change_set_recovery_encryption_and_invalid_execution_id() {
        let f = Fixture::new();
        let mutations = [create("a.txt")];
        let journal = f.prepare(&mutations);
        let bytes = fs::read(f.store.path(&journal.execution_id, "journal").unwrap()).unwrap();
        assert!(!bytes
            .windows(b"synthetic-project".len())
            .any(|w| w == b"synthetic-project"));
        assert!(f.store.read("../../outside").is_err());
        assert!(JournalStore::new(f.base.join("state"), [43; 32])
            .read(&journal.execution_id)
            .is_err());
    }

    #[test]
    fn change_set_recovery_parent_replacement_preserves_external_directory() {
        let f = Fixture::new();
        fs::create_dir(f.scope.root.join("nested")).unwrap();
        fs::write(f.scope.root.join("nested/a.txt"), "original").unwrap();
        let mutation = Mutation {
            kind: "delete".into(),
            path: "nested/a.txt".into(),
            source_path: None,
            bytes: vec![],
            expected_hash: Some(hash(b"original")),
        };
        let mutations = [mutation];
        let mut journal = f.prepare(&mutations);
        f.store.execute(&mut journal, &f.scope, &mutations).unwrap();
        fs::rename(f.scope.root.join("nested"), f.scope.root.join("previous")).unwrap();
        fs::create_dir(f.scope.root.join("nested")).unwrap();
        assert_eq!(
            f.store
                .recover_execution(&journal.execution_id, &f.scope, true)
                .unwrap()
                .outcome(),
            RecoveryOutcome::RollbackConflicted
        );
        assert!(!f.scope.root.join("nested/a.txt").exists());
    }
    #[test]
    fn change_set_recovery_journal_flush_failure_after_write_retains_uncertain_intent() {
        let f = Fixture::new();
        let mutations = [create("a.txt")];
        let mut journal = f.prepare(&mutations);
        fault("storage_flush", 1, false);
        assert!(f.store.execute(&mut journal, &f.scope, &mutations).is_err());
        assert!(f.scope.root.join("a.txt").exists());
        assert_eq!(
            f.restarted()
                .recover_execution(&journal.execution_id, &f.scope, false)
                .unwrap()
                .outcome(),
            RecoveryOutcome::RecoveryRequired
        );
    }
    #[test]
    fn change_set_recovery_internal_staging_crash_never_claims_rollback_or_replays() {
        for stage in ["write_staged", "write_backed_up", "write_replaced"] {
            let f = Fixture::new();
            fs::write(f.scope.root.join("a.txt"), "original").unwrap();
            let mut mutation = create("a.txt");
            mutation.kind = "overwrite_text".into();
            mutation.expected_hash = Some(hash(b"original"));
            let mutations = [mutation];
            let mut journal = f.prepare(&mutations);
            crash_execute(&f, &mut journal, &mutations, stage, 0);
            let files_before = fs::read_dir(&f.scope.root)
                .unwrap()
                .map(|e| e.unwrap().file_name())
                .collect::<Vec<_>>();
            for _ in 0..2 {
                assert_eq!(
                    f.restarted()
                        .recover_execution(&journal.execution_id, &f.scope, false)
                        .unwrap()
                        .outcome(),
                    RecoveryOutcome::RecoveryRequired
                );
                assert_eq!(
                    fs::read_dir(&f.scope.root)
                        .unwrap()
                        .map(|e| e.unwrap().file_name())
                        .collect::<Vec<_>>(),
                    files_before
                );
            }
            let original = if stage == "write_staged" {
                f.scope.root.join("a.txt")
            } else {
                f.scope.root.join(format!(
                    ".agent-{}.bak",
                    journal.steps[0].intent.artifact_id
                ))
            };
            assert_eq!(fs::read_to_string(original).unwrap(), "original");
        }
    }
    #[test]
    fn change_set_recovery_interrupted_rollback_staging_is_uncertain_without_retry() {
        let f = Fixture::new();
        fs::write(f.scope.root.join("a.txt"), "original").unwrap();
        let mut mutation = create("a.txt");
        mutation.kind = "overwrite_text".into();
        mutation.expected_hash = Some(hash(b"original"));
        let mutations = [mutation];
        let mut journal = f.prepare(&mutations);
        f.store.execute(&mut journal, &f.scope, &mutations).unwrap();
        fault("write_staged", 0, true);
        assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| f
            .store
            .recover_execution(&journal.execution_id, &f.scope, true)))
        .is_err());
        let identity = capture(&f.scope, "a.txt").unwrap().state.identity;
        for _ in 0..2 {
            assert_eq!(
                f.restarted()
                    .recover_execution(&journal.execution_id, &f.scope, false)
                    .unwrap()
                    .outcome(),
                RecoveryOutcome::RecoveryRequired
            );
            assert_eq!(capture(&f.scope, "a.txt").unwrap().state.identity, identity);
        }
    }
}
