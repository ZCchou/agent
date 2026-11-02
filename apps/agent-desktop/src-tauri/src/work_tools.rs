use std::{collections::HashMap, fs, io::Write, path::PathBuf};

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectGrant {
    pub project_id: String,
    pub directory: String,
    pub active: bool,
    pub suspended_at: Option<String>,
    pub root_generation: u64,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct PersistedGrants {
    projects: HashMap<String, GrantRecord>,
    #[serde(default)]
    works: HashMap<String, WorkPermissionRecord>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct GrantRecord {
    directory: String,
    active: bool,
    suspended_at: Option<String>,
    #[serde(default = "initial_root_generation")]
    root_generation: u64,
}

fn initial_root_generation() -> u64 {
    1
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ApprovalMode {
    Human,
    AiReview,
    #[default]
    Open,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct WorkPermissionRecord {
    project_id: String,
    root_generation: u64,
    mode: ApprovalMode,
}

#[derive(Debug, Clone, Serialize)]
pub struct WorkPermissionView {
    pub conversation_id: String,
    pub project_id: String,
    pub root_generation: u64,
    pub mode: ApprovalMode,
}

pub struct WorkToolManager {
    store_path: PathBuf,
    grants: PersistedGrants,
    disabled_reason: Option<String>,
}

impl WorkToolManager {
    pub fn load(store_path: PathBuf) -> Result<Self, String> {
        let mut needs_migration = false;
        let grants = if store_path.exists() {
            let text = fs::read_to_string(&store_path)
                .map_err(|_| "Project grants could not be loaded".to_string())?;
            let value: serde_json::Value = serde_json::from_str(&text)
                .map_err(|_| "Project grants are damaged".to_string())?;
            let projects = value
                .get("projects")
                .and_then(|item| item.as_object())
                .ok_or_else(|| "Project grants are damaged".to_string())?;
            let mut migrated = HashMap::new();
            for (project_id, stored) in projects {
                let record = if let Some(directory) = stored.as_str() {
                    needs_migration = true;
                    GrantRecord {
                        directory: directory.to_string(),
                        active: true,
                        suspended_at: None,
                        root_generation: 1,
                    }
                } else {
                    if stored.get("root_generation").is_none() {
                        needs_migration = true;
                    }
                    serde_json::from_value(stored.clone())
                        .map_err(|_| "Project grants are damaged".to_string())?
                };
                migrated.insert(project_id.clone(), record);
            }
            let stored_works = value.get("works");
            if stored_works.is_none() {
                needs_migration = true;
            }
            let works = stored_works
                .cloned()
                .map(serde_json::from_value)
                .transpose()
                .map_err(|_| "Project grants are damaged".to_string())?
                .unwrap_or_default();
            PersistedGrants {
                projects: migrated,
                works,
            }
        } else {
            PersistedGrants::default()
        };
        let manager = Self {
            store_path,
            grants,
            disabled_reason: None,
        };
        if needs_migration {
            manager.persist()?;
        }
        Ok(manager)
    }

    pub fn disabled(store_path: PathBuf, reason: impl Into<String>) -> Self {
        Self {
            store_path,
            grants: PersistedGrants::default(),
            disabled_reason: Some(reason.into()),
        }
    }

    pub fn set_project_directory(
        &mut self,
        project_id: String,
        directory: PathBuf,
    ) -> Result<ProjectGrant, String> {
        self.ensure_available()?;
        let canonical = directory
            .canonicalize()
            .map_err(|_| "The selected directory is unavailable".to_string())?;
        if !canonical.is_dir() {
            return Err("The selected path is not a directory".to_string());
        }
        let visible = canonical.to_string_lossy().into_owned();
        let next_generation = self.grants.projects.get(&project_id).map_or(1, |current| {
            if current.directory == visible {
                current.root_generation
            } else {
                current.root_generation.saturating_add(1)
            }
        });
        let previous = self.grants.clone();
        self.grants.projects.insert(
            project_id.clone(),
            GrantRecord {
                directory: visible.clone(),
                active: true,
                suspended_at: None,
                root_generation: next_generation,
            },
        );
        for permission in self.grants.works.values_mut() {
            if permission.project_id == project_id && permission.root_generation != next_generation
            {
                permission.root_generation = next_generation;
            }
        }
        if let Err(error) = self.persist() {
            self.grants = previous;
            return Err(error);
        }
        Ok(ProjectGrant {
            project_id,
            directory: visible,
            active: true,
            suspended_at: None,
            root_generation: next_generation,
        })
    }

    pub fn project_grant(&self, project_id: &str) -> Option<ProjectGrant> {
        self.grants
            .projects
            .get(project_id)
            .map(|record| ProjectGrant {
                project_id: project_id.to_string(),
                directory: record.directory.clone(),
                active: record.active,
                suspended_at: record.suspended_at.clone(),
                root_generation: record.root_generation,
            })
    }

    pub fn work_permission(
        &self,
        conversation_id: &str,
        project_id: &str,
    ) -> Result<WorkPermissionView, String> {
        self.ensure_available()?;
        let grant = self
            .grants
            .projects
            .get(project_id)
            .ok_or_else(|| "This project has no authorized directory".to_string())?;
        let mode = self
            .grants
            .works
            .get(conversation_id)
            .filter(|permission| {
                permission.project_id == project_id
                    && permission.root_generation == grant.root_generation
            })
            .map_or(ApprovalMode::default(), |permission| permission.mode);
        Ok(WorkPermissionView {
            conversation_id: conversation_id.to_string(),
            project_id: project_id.to_string(),
            root_generation: grant.root_generation,
            mode,
        })
    }

    pub fn set_work_permission(
        &mut self,
        conversation_id: String,
        project_id: String,
        mode: ApprovalMode,
    ) -> Result<WorkPermissionView, String> {
        self.ensure_available()?;
        let grant = self.grants.projects.get(&project_id).ok_or_else(|| {
            "Choose a project directory before changing approval mode".to_string()
        })?;
        if !grant.active {
            return Err("This project's authorized directory is suspended".to_string());
        }
        let root_generation = grant.root_generation;
        let previous = self.grants.clone();
        self.grants.works.insert(
            conversation_id.clone(),
            WorkPermissionRecord {
                project_id: project_id.clone(),
                root_generation,
                mode,
            },
        );
        if let Err(error) = self.persist() {
            self.grants = previous;
            return Err(error);
        }
        Ok(WorkPermissionView {
            conversation_id,
            project_id,
            root_generation,
            mode,
        })
    }

    pub fn reset_work_permission(&mut self, conversation_id: &str) -> Result<(), String> {
        self.ensure_available()?;
        let previous = self.grants.clone();
        if self.grants.works.remove(conversation_id).is_some() {
            if let Err(error) = self.persist() {
                self.grants = previous;
                return Err(error);
            }
        }
        Ok(())
    }

    pub fn suspend_project(&mut self, project_id: &str) -> Result<(), String> {
        self.ensure_available()?;
        let previous = self.grants.clone();
        if let Some(record) = self.grants.projects.get_mut(project_id) {
            record.active = false;
            record.suspended_at = Some(chrono::Utc::now().to_rfc3339());
        }
        if let Err(error) = self.persist() {
            self.grants = previous;
            return Err(error);
        }
        Ok(())
    }

    pub fn restore_project(&mut self, project_id: &str) -> Result<(), String> {
        self.ensure_available()?;
        let previous = self.grants.clone();
        if let Some(record) = self.grants.projects.get_mut(project_id) {
            record.active = true;
            record.suspended_at = None;
        }
        if let Err(error) = self.persist() {
            self.grants = previous;
            return Err(error);
        }
        Ok(())
    }

    pub fn remove_project(&mut self, project_id: &str) -> Result<(), String> {
        self.ensure_available()?;
        let previous = self.grants.clone();
        self.grants.projects.remove(project_id);
        self.grants
            .works
            .retain(|_, permission| permission.project_id != project_id);
        if let Err(error) = self.persist() {
            self.grants = previous;
            return Err(error);
        }
        Ok(())
    }

    fn root(&self, project_id: &str) -> Result<PathBuf, String> {
        self.ensure_available()?;
        let configured = self
            .grants
            .projects
            .get(project_id)
            .ok_or_else(|| "This project has no authorized directory".to_string())?;
        if !configured.active {
            return Err("This project's authorized directory is suspended".to_string());
        }
        // Preserve the granted path: re-canonicalizing here would hide a later junction swap.
        Ok(PathBuf::from(&configured.directory))
    }

    pub fn authorized_root(&self, project_id: &str) -> Result<PathBuf, String> {
        self.root(project_id)
    }

    pub fn authorized_access(
        &self,
        project_id: &str,
        conversation_id: &str,
    ) -> Result<crate::file_access::AuthorizedRoot, String> {
        let root = self.root(project_id)?;
        let grant = self
            .grants
            .projects
            .get(project_id)
            .ok_or_else(|| "The project grant is unavailable".to_string())?;
        Ok(crate::file_access::AuthorizedRoot {
            project_id: project_id.to_string(),
            conversation_id: conversation_id.to_string(),
            root_generation: grant.root_generation,
            policy_version: crate::file_access::POLICY_VERSION,
            root,
            protected_root: self
                .store_path
                .parent()
                .unwrap_or(&self.store_path)
                .to_path_buf(),
        })
    }

    fn persist(&self) -> Result<(), String> {
        let parent = self
            .store_path
            .parent()
            .ok_or_else(|| "Project grant storage is unavailable".to_string())?;
        fs::create_dir_all(parent)
            .map_err(|_| "Project grant storage is unavailable".to_string())?;
        let text = serde_json::to_string_pretty(&self.grants)
            .map_err(|_| "Project grants could not be saved".to_string())?;
        let temporary = parent.join(format!("work-tool-grants-{}.tmp", uuid::Uuid::new_v4()));
        let mut file = fs::File::create(&temporary)
            .map_err(|_| "Project grants could not be saved".to_string())?;
        file.write_all(text.as_bytes())
            .and_then(|_| file.sync_all())
            .map_err(|_| "Project grants could not be saved".to_string())?;
        if !self.store_path.exists() {
            return fs::rename(&temporary, &self.store_path)
                .map_err(|_| "Project grants could not be saved".to_string());
        }
        let backup = parent.join(format!("work-tool-grants-{}.backup", uuid::Uuid::new_v4()));
        fs::rename(&self.store_path, &backup)
            .map_err(|_| "Project grants could not be saved".to_string())?;
        if fs::rename(&temporary, &self.store_path).is_err() {
            let _ = fs::rename(&backup, &self.store_path);
            return Err("Project grants could not be saved".to_string());
        }
        let _ = fs::remove_file(backup);
        Ok(())
    }

    fn ensure_available(&self) -> Result<(), String> {
        if self.disabled_reason.is_some() {
            return Err(
                "tool_system_unavailable: Project authorization storage is unavailable; pure chat remains available"
                    .to_string(),
            );
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use uuid::Uuid;

    fn temp_dir() -> PathBuf {
        let path = std::env::temp_dir().join(format!("agent-tools-{}", Uuid::new_v4()));
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[cfg(windows)]
    #[test]
    fn authorization_snapshot_preserves_grant_path_for_reparse_verification() {
        let fixture = temp_dir();
        let project = fixture.join("project");
        let outside = fixture.join("outside");
        fs::create_dir(&project).unwrap();
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("note.txt"), "synthetic outside").unwrap();
        let mut manager = WorkToolManager::load(fixture.join("state/grants.json")).unwrap();
        manager
            .set_project_directory("p".into(), project.clone())
            .unwrap();
        let original = manager.authorized_root("p").unwrap();
        fs::rename(&project, fixture.join("previous")).unwrap();
        let result = std::process::Command::new("cmd")
            .args(["/C", "mklink", "/J"])
            .arg(&project)
            .arg(&outside)
            .output()
            .unwrap();
        assert!(result.status.success());
        let scope = manager.authorized_access("p", "c").unwrap();
        let policy = crate::file_access::FileAccessPolicy;
        let access = policy.open_read(&scope, &policy.validate_relative("note.txt").unwrap());
        fs::remove_dir(&project).unwrap();
        assert_eq!(scope.root, original);
        assert_eq!(
            (
                scope.project_id.as_str(),
                scope.conversation_id.as_str(),
                scope.root_generation
            ),
            ("p", "c", 1)
        );
        assert!(access.is_err());
        fs::remove_dir_all(fixture).unwrap();
    }

    #[test]
    fn grants_and_work_modes_survive_restart() {
        let root = temp_dir();
        let store = root.join("grants.json");
        let mut manager = WorkToolManager::load(store.clone()).unwrap();
        manager
            .set_project_directory("p1".to_string(), root.clone())
            .unwrap();
        manager
            .set_work_permission("c1".to_string(), "p1".to_string(), ApprovalMode::Open)
            .unwrap();
        let restarted = WorkToolManager::load(store).unwrap();
        assert!(restarted.project_grant("p1").is_some());
        assert_eq!(
            restarted.work_permission("c1", "p1").unwrap().mode,
            ApprovalMode::Open
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn suspended_project_keeps_but_disables_grant_until_restore() {
        let root = temp_dir();
        let store = root.join("grants.json");
        let mut manager = WorkToolManager::load(store.clone()).unwrap();
        manager
            .set_project_directory("p1".to_string(), root.clone())
            .unwrap();
        manager
            .set_work_permission("human-work".into(), "p1".into(), ApprovalMode::Human)
            .unwrap();
        manager
            .set_work_permission("ai-work".into(), "p1".into(), ApprovalMode::AiReview)
            .unwrap();
        manager.suspend_project("p1").unwrap();
        assert!(!manager.project_grant("p1").unwrap().active);
        assert!(manager.authorized_root("p1").is_err());
        let mut restarted = WorkToolManager::load(store).unwrap();
        assert!(!restarted.project_grant("p1").unwrap().active);
        restarted.restore_project("p1").unwrap();
        assert!(restarted.project_grant("p1").unwrap().active);
        assert_eq!(
            restarted.work_permission("human-work", "p1").unwrap().mode,
            ApprovalMode::Human
        );
        assert_eq!(
            restarted.work_permission("ai-work", "p1").unwrap().mode,
            ApprovalMode::AiReview
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn changing_directory_preserves_explicit_mode_with_new_authority_generation() {
        let root = temp_dir();
        let second = temp_dir();
        let mut manager = WorkToolManager::load(root.join("grants.json")).unwrap();
        manager
            .set_project_directory("p1".to_string(), root.clone())
            .unwrap();
        manager
            .set_work_permission("c1".to_string(), "p1".to_string(), ApprovalMode::Open)
            .unwrap();
        manager
            .set_project_directory("p1".to_string(), second.clone())
            .unwrap();
        assert_eq!(
            manager.work_permission("c1", "p1").unwrap().mode,
            ApprovalMode::Open
        );
        assert_eq!(
            manager.work_permission("c1", "p1").unwrap().root_generation,
            2
        );
        manager
            .set_work_permission("c1".into(), "p1".into(), ApprovalMode::Human)
            .unwrap();
        manager
            .set_project_directory("p1".into(), root.clone())
            .unwrap();
        assert_eq!(
            manager.work_permission("c1", "p1").unwrap().mode,
            ApprovalMode::Human
        );
        assert_eq!(
            manager.work_permission("c1", "p1").unwrap().root_generation,
            3
        );
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(second).unwrap();
    }

    #[test]
    fn legacy_structured_grant_is_migrated_and_persisted() {
        let root = temp_dir();
        let store = root.join("grants.json");
        fs::write(
            &store,
            serde_json::json!({
                "projects": {
                    "p1": {"directory": root, "active": true, "suspended_at": null}
                }
            })
            .to_string(),
        )
        .unwrap();
        let manager = WorkToolManager::load(store.clone()).unwrap();
        assert_eq!(manager.project_grant("p1").unwrap().root_generation, 1);
        let migrated: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&store).unwrap()).unwrap();
        assert_eq!(migrated["projects"]["p1"]["root_generation"], 1);
        assert!(migrated.get("works").is_some());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn damaged_grant_migration_does_not_rewrite_the_source_file() {
        let root = temp_dir();
        let store = root.join("grants.json");
        let original = r#"{"projects":{"p1":{"directory":false}}}"#;
        fs::write(&store, original).unwrap();
        assert!(WorkToolManager::load(store.clone()).is_err());
        assert_eq!(fs::read_to_string(&store).unwrap(), original);
        fs::remove_dir_all(root).unwrap();
    }
}
