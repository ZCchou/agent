use crate::work_tools::ApprovalMode;
use std::{
    collections::{HashMap, HashSet},
    sync::{Arc, Mutex},
};
use tokio::sync::oneshot;
#[derive(Default)]
struct State {
    seen: HashSet<String>,
    pending: HashMap<String, oneshot::Sender<bool>>,
}
#[derive(Default, Clone)]
pub struct TerminalService {
    state: Arc<Mutex<State>>,
}
pub struct PendingApproval {
    service: TerminalService,
    id: String,
    pub receiver: oneshot::Receiver<bool>,
}
impl Drop for PendingApproval {
    fn drop(&mut self) {
        self.service.state.lock().unwrap().pending.remove(&self.id);
    }
}
pub fn requires_approval(mode: ApprovalMode) -> bool {
    mode != ApprovalMode::Open
}
impl TerminalService {
    pub fn claim(&self, id: &str) -> bool {
        self.state.lock().unwrap().seen.insert(id.into())
    }
    pub fn pending(&self, id: &str) -> Result<PendingApproval, String> {
        let mut state = self.state.lock().unwrap();
        if !state.seen.contains(id) || state.pending.contains_key(id) {
            return Err("approval_missing: Operation is not awaiting a decision".into());
        }
        let (sender, receiver) = oneshot::channel();
        state.pending.insert(id.into(), sender);
        Ok(PendingApproval {
            service: self.clone(),
            id: id.into(),
            receiver,
        })
    }
    pub fn resolve(&self, id: &str, approve: bool) -> Result<(), String> {
        self.state
            .lock()
            .unwrap()
            .pending
            .remove(id)
            .ok_or("approval_missing: Approval expired or was already used")?
            .send(approve)
            .map_err(|_| "approval_missing: Approval expired".into())
    }
}
#[tauri::command]
pub fn resolve_terminal_operation(
    service: tauri::State<'_, TerminalService>,
    operation_id: String,
    approve: bool,
) -> Result<(), String> {
    service.resolve(&operation_id, approve)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn decisions_are_one_shot_and_open_only_skips_manual_review() {
        let service = TerminalService::default();
        assert!(service.claim("op"));
        assert!(!service.claim("op"));
        let mut approval = service.pending("op").unwrap();
        service.resolve("op", true).unwrap();
        assert!(service.resolve("op", false).is_err());
        assert!(approval.receiver.try_recv().unwrap());
        assert!(!requires_approval(crate::work_tools::ApprovalMode::Open));
        assert!(requires_approval(crate::work_tools::ApprovalMode::Human));
        assert!(requires_approval(crate::work_tools::ApprovalMode::AiReview));
    }
    #[test]
    fn dropped_approval_cannot_be_used_or_replayed() {
        let service = TerminalService::default();
        assert!(service.claim("op"));
        let approval = service.pending("op").unwrap();
        drop(approval);
        assert!(service.resolve("op", true).is_err());
        assert!(!service.claim("op"));
    }
}
