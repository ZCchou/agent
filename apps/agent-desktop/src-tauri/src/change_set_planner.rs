//! Canonical, read-only planning over Host-verified filesystem observations.
use crate::{
    agent_execution::FileMutation,
    change_set_recovery::{capture, ObjectState},
    file_access::{windows_path_eq, AuthorizedRoot, FileAccessPolicy},
    file_security::{classify_mutations, OperationRisk},
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};

pub type PlanError = String;
pub(crate) const MAX_MUTATIONS: usize = 20;
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Precondition {
    pub kind: String,
    pub identity: Option<String>,
    pub sha256: Option<String>,
}
impl From<ObjectState> for Precondition {
    fn from(state: ObjectState) -> Self {
        Self {
            kind: state.kind,
            identity: state
                .identity
                .map(|id| format!("{:016x}:{:032x}", id.volume, id.file)),
            sha256: state.sha256,
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PlannedMutation {
    pub operation_id: String,
    pub mutation: FileMutation,
    pub depends_on: Vec<String>,
    pub preconditions: BTreeMap<String, Precondition>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChangeSetPlan {
    pub plan_id: String,
    pub plan_hash: String,
    pub policy_version: u32,
    pub root_generation: u64,
    pub steps: Vec<PlannedMutation>,
    pub effective_risk: OperationRisk,
}
#[derive(Debug, Clone)]
pub struct PlanApproval {
    pub plan_id: String,
    pub plan_hash: String,
    pub approved_operation_ids: Vec<String>,
}
fn error(code: &str) -> String {
    format!("{code}: File change plan could not be verified")
}
fn hash(value: &impl Serialize) -> Result<String, String> {
    Ok(format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(value).map_err(|_| error("invalid_change_plan"))?)
    ))
}
pub fn normalize(path: &str) -> Result<String, String> {
    let path = FileAccessPolicy.validate_relative(path)?;
    let path = path.as_path().to_string_lossy().replace('\\', "/");
    if path.is_empty() {
        return Err(error("forbidden_path"));
    }
    Ok(path)
}
fn parent(path: &str) -> &str {
    path.rsplit_once('/')
        .map(|(parent, _)| parent)
        .unwrap_or(".")
}
fn overlap(a: &str, b: &str) -> bool {
    windows_path_eq(a, b) || descendant(a, b) || descendant(b, a)
}

fn descendant(path: &str, ancestor: &str) -> bool {
    path.match_indices('/')
        .any(|(end, _)| windows_path_eq(&path[..end], ancestor))
}

pub fn compile_change_set(
    root: &AuthorizedRoot,
    mutations: &[FileMutation],
) -> Result<ChangeSetPlan, PlanError> {
    if mutations.is_empty() || mutations.len() > MAX_MUTATIONS {
        return Err(error("tool_limit_exceeded"));
    }
    let mut normalized = BTreeMap::new();
    for mutation in mutations {
        let mut mutation = mutation.clone();
        mutation.path = normalize(&mutation.path)?;
        mutation.source_path = mutation.source_path.as_deref().map(normalize).transpose()?;
        if normalized
            .keys()
            .any(|path: &String| windows_path_eq(path, &mutation.path))
        {
            return Err(error("conflicting_change_set"));
        }
        normalized.insert(mutation.path.clone(), mutation);
    }
    // The only allowed overlap is an explicit mkdir ancestor of mkdir/create_text.
    for (path, mutation) in &normalized {
        for (other_path, other) in &normalized {
            if path == other_path {
                continue;
            }
            if overlap(path, other_path)
                && !(descendant(other_path, path)
                    && mutation.kind == "mkdir"
                    && matches!(other.kind.as_str(), "mkdir" | "create_text"))
                && !(descendant(path, other_path)
                    && other.kind == "mkdir"
                    && matches!(mutation.kind.as_str(), "mkdir" | "create_text"))
            {
                return Err(error("conflicting_change_set"));
            }
        }
        if let Some(source) = &mutation.source_path {
            if normalized.keys().any(|target| overlap(source, target)) {
                return Err(error("conflicting_change_set"));
            }
            for (other_path, other) in &normalized {
                if path != other_path
                    && other
                        .source_path
                        .as_ref()
                        .is_some_and(|other_source| overlap(source, other_source))
                {
                    return Err(error("conflicting_change_set"));
                }
            }
        }
    }
    let mut steps = BTreeMap::new();
    for (path, mut mutation) in normalized.clone() {
        let mut preconditions: BTreeMap<String, Precondition> = BTreeMap::new();
        for leaf in std::iter::once(&path).chain(mutation.source_path.iter()) {
            let mut current = leaf.as_str();
            loop {
                preconditions.insert(current.to_string(), capture(root, current)?.state.into());
                if current == "." {
                    break;
                }
                current = parent(current);
            }
        }
        let target = &preconditions[&path];
        let mut depends_on = Vec::new();
        let mut ancestor = parent(&path);
        while ancestor != "." {
            match preconditions[ancestor].kind.as_str() {
                "directory" => {}
                "missing"
                    if matches!(mutation.kind.as_str(), "mkdir" | "create_text")
                        && normalized.iter().any(|(path, m)| {
                            windows_path_eq(path, ancestor) && m.kind == "mkdir"
                        }) =>
                {
                    depends_on.push(
                        normalized
                            .keys()
                            .find(|path| windows_path_eq(path, ancestor))
                            .unwrap()
                            .clone(),
                    );
                }
                _ => return Err(error("missing_parent_dependency")),
            }
            ancestor = parent(ancestor);
        }
        let before = mutation
            .source_path
            .as_ref()
            .map(|s| &preconditions[s])
            .unwrap_or(target);
        if mutation
            .expected_hash
            .as_ref()
            .is_some_and(|hash| Some(hash) != before.sha256.as_ref())
        {
            return Err(error("file_changed_since_read"));
        }
        mutation.expected_hash = before.sha256.clone();
        match mutation.kind.as_str() {
            "mkdir" | "create_text" if target.kind != "missing" => {
                return Err(error("file_already_exists"))
            }
            "overwrite_text" | "patch_text" if target.kind != "file" => {
                return Err(error("path_not_found"))
            }
            "delete" if target.kind == "missing" => return Err(error("path_not_found")),
            "copy" | "move"
                if mutation.source_path.is_none()
                    || before.kind != "file"
                    || target.kind != "missing" =>
            {
                return Err(error("conflicting_change_set"))
            }
            "mkdir" | "create_text" | "overwrite_text" | "patch_text" | "delete" | "copy"
            | "move" => {}
            _ => return Err(error("invalid_tool_call")),
        }
        let operation_id = format!("op-{}", hash(&mutation)?);
        steps.insert(
            path,
            PlannedMutation {
                operation_id,
                mutation,
                depends_on,
                preconditions,
            },
        );
    }
    let ids: BTreeMap<_, _> = steps
        .iter()
        .map(|(path, step)| (path.clone(), step.operation_id.clone()))
        .collect();
    for step in steps.values_mut() {
        step.depends_on = step
            .depends_on
            .iter()
            .map(|path| ids[path].clone())
            .collect();
        step.depends_on.sort();
    }
    let mut ordered = Vec::new();
    let mut emitted = BTreeSet::new();
    while !steps.is_empty() {
        let key = steps
            .iter()
            .find(|(_, step)| step.depends_on.iter().all(|id| emitted.contains(id)))
            .map(|(path, _)| path.clone())
            .ok_or_else(|| error("cyclic_change_set"))?;
        let step = steps.remove(&key).expect("selected step exists");
        emitted.insert(step.operation_id.clone());
        ordered.push(step);
    }
    let raw = ordered
        .iter()
        .map(|step| serde_json::to_value(&step.mutation).map_err(|_| error("invalid_change_plan")))
        .collect::<Result<Vec<_>, _>>()?;
    let risk = classify_mutations(&raw);
    if risk == OperationRisk::Forbidden {
        return Err(error("forbidden_operation"));
    }
    let plan_hash = hash(&(root.policy_version, root.root_generation, &ordered, risk))?;
    Ok(ChangeSetPlan {
        plan_id: format!("plan-{plan_hash}"),
        plan_hash,
        policy_version: root.policy_version,
        root_generation: root.root_generation,
        steps: ordered,
        effective_risk: risk,
    })
}
impl ChangeSetPlan {
    pub fn approval(&self) -> PlanApproval {
        PlanApproval {
            plan_id: self.plan_id.clone(),
            plan_hash: self.plan_hash.clone(),
            approved_operation_ids: self.steps.iter().map(|s| s.operation_id.clone()).collect(),
        }
    }
    pub fn select(&self, approval: &PlanApproval) -> Result<Vec<FileMutation>, String> {
        if approval.plan_id != self.plan_id || approval.plan_hash != self.plan_hash {
            return Err(error("approval_plan_mismatch"));
        }
        let selected: BTreeSet<_> = approval.approved_operation_ids.iter().collect();
        if selected.is_empty()
            || selected.len() != approval.approved_operation_ids.len()
            || selected
                .iter()
                .any(|id| !self.steps.iter().any(|s| &s.operation_id == *id))
        {
            return Err(error("invalid_approval_selection"));
        }
        let mut mutations = Vec::new();
        for step in &self.steps {
            if selected.contains(&step.operation_id) {
                if step.depends_on.iter().any(|id| !selected.contains(id)) {
                    return Err(error("approval_missing_dependency"));
                }
                mutations.push(step.mutation.clone());
            }
        }
        Ok(mutations)
    }
    pub fn validate(&self, root: &AuthorizedRoot) -> Result<(), String> {
        if root.root_generation != self.root_generation
            || root.policy_version != self.policy_version
        {
            return Err(error("scope_changed"));
        }
        for step in &self.steps {
            for (path, state) in &step.preconditions {
                if Precondition::from(capture(root, path)?.state) != *state {
                    return Err(error("file_changed_since_read"));
                }
            }
        }
        Ok(())
    }
    pub fn execution_hash(&self, mutations: &[FileMutation]) -> Result<String, String> {
        let mut ids = Vec::new();
        for mutation in mutations {
            let id = format!("op-{}", hash(mutation)?);
            if !self.steps.iter().any(|step| step.operation_id == id) {
                return Err(error("execution_plan_mismatch"));
            }
            ids.push(id);
        }
        ids.sort();
        self.select(&PlanApproval {
            plan_id: self.plan_id.clone(),
            plan_hash: self.plan_hash.clone(),
            approved_operation_ids: ids.clone(),
        })?;
        hash(&(&self.plan_hash, ids))
    }
    pub fn redact(&mut self) {
        for step in &mut self.steps {
            step.mutation.content = None;
            step.mutation.old_text = None;
            step.mutation.new_text = None;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};
    use std::{fs, path::PathBuf};
    struct Fixture {
        path: PathBuf,
        root: AuthorizedRoot,
    }
    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!("agent-h4-{}", uuid::Uuid::new_v4()));
            fs::create_dir(&path).unwrap();
            let path = path.canonicalize().unwrap();
            let root = AuthorizedRoot::for_host_path(&path, std::path::Path::new(""));
            Self { path, root }
        }
        fn compile(&self, value: Value) -> Result<ChangeSetPlan, String> {
            compile_change_set(
                &self.root,
                &serde_json::from_value::<Vec<FileMutation>>(value).unwrap(),
            )
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.path).unwrap();
        }
    }
    #[test]
    fn execution_spelling_preserves_unicode_and_distinct_windows_names() {
        let f = Fixture::new();
        let plan = f
            .compile(json!([{"kind":"create_text","path":"README.md","content":"x"}]))
            .unwrap();
        assert_eq!(plan.steps[0].mutation.path, "README.md");
        fs::write(f.path.join("İ.txt"), "original").unwrap();
        let patch = f.compile(json!([{"kind":"patch_text","path":"İ.txt","old_text":"original","new_text":"next"}])).unwrap();
        assert_eq!(patch.steps[0].mutation.path, "İ.txt");
        let copy = f
            .compile(json!([{"kind":"copy","source_path":"İ.txt","path":"Copy.txt"}]))
            .unwrap();
        assert_eq!(copy.steps[0].mutation.source_path.as_deref(), Some("İ.txt"));
        let distinct = f.compile(json!([{"kind":"create_text","path":"İ-new.txt"},{"kind":"create_text","path":"i\u{0307}-new.txt"}])).unwrap();
        assert_eq!(distinct.steps.len(), 2);
    }
    #[test]
    fn change_set_planner_permutations_preserve_hash_and_bind_exact_spelling() {
        let f = Fixture::new();
        let values = vec![
            json!({"kind":"create_text","path":"a/b/n.txt","content":"expected"}),
            json!({"kind":"mkdir","path":"a/b"}),
            json!({"kind":"mkdir","path":"a"}),
        ];
        let first = f.compile(json!(values)).unwrap();
        for permutation in [
            [0, 1, 2],
            [0, 2, 1],
            [1, 0, 2],
            [1, 2, 0],
            [2, 0, 1],
            [2, 1, 0],
        ] {
            let plan = f
                .compile(json!(permutation.map(|i| values[i].clone())))
                .unwrap();
            assert_eq!(first.plan_hash, plan.plan_hash);
            assert_eq!(first.plan_id, plan.plan_id);
            assert_eq!(
                plan.steps
                    .iter()
                    .map(|s| s.mutation.path.as_str())
                    .collect::<Vec<_>>(),
                ["a", "a/b", "a/b/n.txt"]
            );
        }
        let aliases=f.compile(json!([{"kind":"mkdir","path":"./A"},{"kind":"mkdir","path":"A\\B"},{"kind":"create_text","path":"a/b/N.txt","content":"expected"}])).unwrap();
        assert_ne!(first.plan_hash, aliases.plan_hash);
        assert_eq!(aliases.steps[2].depends_on.len(), 2);
        assert_eq!(aliases.steps[2].mutation.path, "a/b/N.txt");
        assert_eq!(fs::read_dir(&f.path).unwrap().count(), 0);
        assert_eq!(first.steps[2].preconditions["a/b"].kind, "missing");
        assert!(first.steps[0].preconditions["."]
            .identity
            .as_ref()
            .unwrap()
            .contains(':'));
    }
    #[test]
    fn change_set_planner_rejects_ambiguous_graphs_and_aliases_without_writes() {
        let f = Fixture::new();
        fs::write(f.path.join("file"), "original").unwrap();
        fs::create_dir(f.path.join("dir")).unwrap();
        fs::write(f.path.join("dir/child"), "original").unwrap();
        for values in [
            json!([{"kind":"mkdir","path":"file/child"}]),
            json!([{"kind":"create_text","path":"absent/child","content":"x"}]),
            json!([{"kind":"create_text","path":"x"},{"kind":"create_text","path":"X"}]),
            json!([{"kind":"mkdir","path":"x."}]),
            json!([{"kind":"mkdir","path":"CON"}]),
            json!([{"kind":"mkdir","path":"dir/file:stream"}]),
            json!([{"kind":"delete","path":"dir"},{"kind":"overwrite_text","path":"dir/child"}]),
            json!([{"kind":"move","source_path":"file","path":"x"},{"kind":"move","source_path":"x","path":"file"}]),
            json!([{"kind":"copy","source_path":"file","path":"x"},{"kind":"delete","path":"file"}]),
            json!([{"kind":"copy","source_path":"file","path":"dir/child"}]),
            json!([{"kind":"create_text","path":"file"}]),
            json!([{"kind":"mkdir","path":"dir"}]),
            json!([{"kind":"move","source_path":"file","path":"a"},{"kind":"move","source_path":"file","path":"b"}]),
            json!([{"kind":"mkdir","path":"a"},{"kind":"copy","source_path":"file","path":"a/n"}]),
        ] {
            assert!(
                f.compile(values.clone()).is_err(),
                "conflicting plan accepted: {values}"
            );
        }
        assert_eq!(fs::read_to_string(f.path.join("file")).unwrap(), "original");
        assert_eq!(
            fs::read_to_string(f.path.join("dir/child")).unwrap(),
            "original"
        );
        assert_eq!(fs::read_dir(&f.path).unwrap().count(), 2);
    }
    #[test]
    fn change_set_planner_subset_requires_exact_binding_and_dependency_closure() {
        let f = Fixture::new();
        let plan=f.compile(json!([{"kind":"mkdir","path":"a"},{"kind":"create_text","path":"a/n"},{"kind":"create_text","path":"independent"}])).unwrap();
        let mut approval = plan.approval();
        let full_hash = plan
            .execution_hash(&plan.select(&approval).unwrap())
            .unwrap();
        approval.approved_operation_ids.reverse();
        assert_eq!(
            full_hash,
            plan.execution_hash(&plan.select(&approval).unwrap())
                .unwrap()
        );
        let child = plan
            .steps
            .iter()
            .find(|s| s.mutation.path == "a/n")
            .unwrap();
        approval.approved_operation_ids = vec![child.operation_id.clone()];
        assert!(plan
            .select(&approval)
            .unwrap_err()
            .starts_with("approval_missing_dependency:"));
        approval
            .approved_operation_ids
            .extend(child.depends_on.clone());
        let selected = plan.select(&approval).unwrap();
        assert_eq!(selected.len(), 2);
        assert_ne!(full_hash, plan.execution_hash(&selected).unwrap());
        approval
            .approved_operation_ids
            .push(child.operation_id.clone());
        assert!(plan.select(&approval).is_err());
        approval = plan.approval();
        approval.plan_hash.push('x');
        assert!(plan.select(&approval).is_err());
        approval = plan.approval();
        approval.plan_id.push('x');
        assert!(plan.select(&approval).is_err());
        approval = plan.approval();
        approval.approved_operation_ids = vec!["forged".into()];
        assert!(plan.select(&approval).is_err());
        approval.approved_operation_ids.clear();
        assert!(plan.select(&approval).is_err());
    }
    #[test]
    fn change_set_planner_authority_and_identity_changes_are_stale() {
        let f = Fixture::new();
        fs::write(f.path.join("existing"), "original").unwrap();
        let plan = f
            .compile(json!([{"kind":"overwrite_text","path":"existing","content":"next"}]))
            .unwrap();
        let mut revoked = f.root.clone();
        revoked.root_generation += 1;
        assert!(plan.validate(&revoked).is_err());
        let mut changed_policy = f.root.clone();
        changed_policy.policy_version += 1;
        assert!(plan.validate(&changed_policy).is_err());
        fs::rename(f.path.join("existing"), f.path.join("old")).unwrap();
        fs::write(f.path.join("existing"), "original").unwrap();
        assert!(plan.validate(&f.root).is_err());
        assert_ne!(
            plan.plan_hash,
            f.compile(json!([{"kind":"overwrite_text","path":"existing","content":"next"}]))
                .unwrap()
                .plan_hash
        );
    }
}
