use crate::attachment_store::{AttachmentManifest, AttachmentStore, RejectedAttachmentDescriptor};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::Mutex,
};
use tauri::{AppHandle, Manager, State};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RestartDraft {
    pub conversation_id: Option<String>,
    pub kind: String,
    pub project_id: Option<String>,
    pub chat_group_id: Option<String>,
    pub category: String,
    pub text: String,
    pub attachment_ids: Vec<String>,
    pub rejected_ids: Vec<String>,
    pub references: Vec<Value>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Checkpoint {
    version: u32,
    update_id: String,
    draft: RestartDraft,
    attachments: Vec<AttachmentManifest>,
    rejected_attachments: Vec<RejectedAttachmentDescriptor>,
}

#[derive(Serialize)]
pub struct RestoredUpdateDraft {
    update_id: String,
    draft: RestartDraft,
    attachments: Vec<AttachmentManifest>,
    rejected_attachments: Vec<RejectedAttachmentDescriptor>,
    missing_attachments: usize,
}

fn checkpoint_path(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_data_dir()
        .map(|root| root.join("update-draft-v1.json"))
        .map_err(|_| "app_update_draft_failed".into())
}

pub fn save(
    path: &Path,
    update_id: &str,
    draft: Option<RestartDraft>,
    store: &AttachmentStore,
) -> Result<(), String> {
    // Never replace an unacknowledged draft from a previous update attempt.
    if path.exists() {
        return Err("app_update_draft_failed".into());
    }
    let Some(draft) = draft else {
        return Ok(());
    };
    if !matches!(draft.kind.as_str(), "standard_chat" | "project_work")
        || draft.text.chars().count() > 20000
        || draft.references.len() > 8
    {
        return Err("app_update_draft_failed".into());
    }
    let checkpoint = Checkpoint {
        version: 1,
        update_id: update_id.to_string(),
        attachments: store
            .resolve(&draft.attachment_ids)
            .map_err(|_| "app_update_draft_failed")?,
        rejected_attachments: store
            .resolve_rejections(&draft.rejected_ids)
            .map_err(|_| "app_update_draft_failed")?,
        draft,
    };
    let bytes = serde_json::to_vec(&checkpoint).map_err(|_| "app_update_draft_failed")?;
    if bytes.len() > 1024 * 1024 {
        return Err("app_update_draft_failed".into());
    }
    let temporary = path.with_extension(format!("{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| -> std::io::Result<()> {
        let mut file = fs::File::create(&temporary)?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temporary, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result.map_err(|_| "app_update_draft_failed".into())
}

fn load(path: &Path, store: &mut AttachmentStore) -> Result<Option<RestoredUpdateDraft>, String> {
    if !path.exists() {
        return Ok(None);
    }
    if fs::metadata(path)
        .map_err(|_| "app_update_draft_failed")?
        .len()
        > 1024 * 1024
    {
        return Err("app_update_draft_failed".into());
    }
    let checkpoint: Checkpoint =
        serde_json::from_slice(&fs::read(path).map_err(|_| "app_update_draft_failed")?)
            .map_err(|_| "app_update_draft_failed")?;
    if checkpoint.version != 1 {
        return Err("app_update_draft_failed".into());
    }
    let (attachments, rejected_attachments, missing_attachments) =
        store.restore_update_staging(checkpoint.attachments, checkpoint.rejected_attachments);
    Ok(Some(RestoredUpdateDraft {
        update_id: checkpoint.update_id,
        draft: checkpoint.draft,
        attachments,
        rejected_attachments,
        missing_attachments,
    }))
}

#[tauri::command]
pub fn load_update_draft(
    app: AppHandle,
    store: State<'_, Mutex<AttachmentStore>>,
) -> Result<Option<RestoredUpdateDraft>, String> {
    let mut store = store.lock().map_err(|_| "app_update_draft_failed")?;
    load_for_mode(
        &checkpoint_path(&app)?,
        &mut store,
        crate::program_update_session::is_health_probe(),
    )
}

#[tauri::command]
pub fn ack_update_draft(app: AppHandle, update_id: String) -> Result<(), String> {
    ack_for_mode(
        &checkpoint_path(&app)?,
        &update_id,
        crate::program_update_session::is_health_probe(),
    )
}

fn load_for_mode(
    path: &Path,
    store: &mut AttachmentStore,
    health_probe: bool,
) -> Result<Option<RestoredUpdateDraft>, String> {
    if health_probe {
        return Ok(None);
    }
    load(path, store)
}

fn ack_for_mode(path: &Path, id: &str, health_probe: bool) -> Result<(), String> {
    if health_probe {
        return Err("app_update_health_probe".into());
    }
    remove_owned(path, id)
}

fn remove_owned(path: &Path, update_id: &str) -> Result<(), String> {
    if !path.exists() {
        return Ok(());
    }
    if fs::metadata(path)
        .map_err(|_| "app_update_draft_failed")?
        .len()
        > 1024 * 1024
    {
        return Err("app_update_draft_failed".into());
    }
    let saved: Checkpoint =
        serde_json::from_slice(&fs::read(path).map_err(|_| "app_update_draft_failed")?)
            .map_err(|_| "app_update_draft_failed")?;
    if saved.update_id != update_id {
        return Err("app_update_stale".into());
    }
    fs::remove_file(path).map_err(|_| "app_update_draft_failed".into())
}

pub fn remove_for_app(app: &AppHandle, update_id: &str) -> Result<(), String> {
    remove_owned(&checkpoint_path(app)?, update_id)
}

pub fn save_for_app(
    app: &AppHandle,
    update_id: &str,
    draft: Option<RestartDraft>,
) -> Result<(), String> {
    let store = app.state::<Mutex<AttachmentStore>>();
    let store = store.lock().map_err(|_| "app_update_draft_failed")?;
    save(&checkpoint_path(app)?, update_id, draft, &store)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn health_probe_cannot_consume_or_acknowledge_a_restart_draft() {
        let root =
            std::env::temp_dir().join(format!("agent-update-probe-{}", uuid::Uuid::new_v4()));
        let mut store = AttachmentStore::new(root.clone()).unwrap();
        let path = root.join("checkpoint.json");
        let draft = RestartDraft {
            conversation_id: None,
            kind: "standard_chat".into(),
            project_id: None,
            chat_group_id: None,
            category: "casual".into(),
            text: "keep unsent draft".into(),
            attachment_ids: vec![],
            rejected_ids: vec![],
            references: vec![],
        };
        save(&path, "probe", Some(draft), &store).unwrap();
        let before = fs::read(&path).unwrap();
        assert!(load_for_mode(&path, &mut store, true).unwrap().is_none());
        assert!(ack_for_mode(&path, "probe", true).is_err());
        assert_eq!(fs::read(&path).unwrap(), before);
        assert_eq!(
            load_for_mode(&path, &mut store, false)
                .unwrap()
                .unwrap()
                .draft
                .text,
            "keep unsent draft"
        );
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn unacknowledged_checkpoint_cannot_be_deleted_or_replaced_by_another_update() {
        let root =
            std::env::temp_dir().join(format!("agent-update-draft-{}", uuid::Uuid::new_v4()));
        let store = AttachmentStore::new(root.clone()).unwrap();
        let path = root.join("checkpoint.json");
        let draft = RestartDraft {
            conversation_id: None,
            kind: "standard_chat".into(),
            project_id: None,
            chat_group_id: None,
            category: "casual".into(),
            text: "keep".into(),
            attachment_ids: vec![],
            rejected_ids: vec![],
            references: vec![],
        };
        save(&path, "first", Some(draft.clone()), &store).unwrap();
        assert!(save(&path, "second", None, &store).is_err());
        assert!(save(&path, "second", Some(draft), &store).is_err());
        assert_eq!(
            remove_owned(&path, "second").unwrap_err(),
            "app_update_stale"
        );
        assert_eq!(
            load(&path, &mut AttachmentStore::new(root.clone()).unwrap())
                .unwrap()
                .unwrap()
                .draft
                .text,
            "keep"
        );
        remove_owned(&path, "first").unwrap();
        assert!(!path.exists());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn restart_preserves_text_and_owned_attachment_and_ack_is_separate() {
        let root =
            std::env::temp_dir().join(format!("agent-update-draft-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("notes.txt");
        fs::write(&source, "unsent file").unwrap();
        let mut store = AttachmentStore::new(root.clone()).unwrap();
        let file = store.import_paths(&[source]).accepted.remove(0);
        let draft = RestartDraft {
            conversation_id: None,
            kind: "standard_chat".into(),
            project_id: None,
            chat_group_id: None,
            category: "casual".into(),
            text: "unsent question".into(),
            attachment_ids: vec![file.id.clone()],
            rejected_ids: vec![],
            references: vec![],
        };
        let path = root.join("update-draft-v1.json");
        save(&path, "update-1", Some(draft), &store).unwrap();
        let mut restarted = AttachmentStore::new(root.clone()).unwrap();
        let restored = load(&path, &mut restarted).unwrap().unwrap();
        assert_eq!(restored.draft.text, "unsent question");
        assert_eq!(restored.attachments[0].id, file.id);
        assert_eq!(
            restarted.preview(&file.id).unwrap().content.as_deref(),
            Some("unsent file")
        );
        assert!(path.exists());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn unknown_staged_identity_cannot_replace_an_existing_checkpoint() {
        let root =
            std::env::temp_dir().join(format!("agent-update-draft-{}", uuid::Uuid::new_v4()));
        let store = AttachmentStore::new(root.clone()).unwrap();
        let path = root.join("checkpoint.json");
        fs::write(&path, "previous checkpoint").unwrap();
        let draft = RestartDraft {
            conversation_id: None,
            kind: "standard_chat".into(),
            project_id: None,
            chat_group_id: None,
            category: "casual".into(),
            text: "keep".into(),
            attachment_ids: vec!["not-owned".into()],
            rejected_ids: vec![],
            references: vec![],
        };
        assert!(save(&path, "u", Some(draft), &store).is_err());
        assert_eq!(fs::read_to_string(path).unwrap(), "previous checkpoint");
        fs::remove_dir_all(root).unwrap();
    }
}
