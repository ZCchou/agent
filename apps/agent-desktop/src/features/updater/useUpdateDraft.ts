import { useCallback, useEffect, useRef } from "react";
import { useApp } from "../../contexts/AppContext";
import { useAppUpdate } from "./AppUpdateProvider";
import type { Conversation } from "../../types/domain";
import type { RestartDraft } from "../../types/updater";
import type { useAttachmentController } from "../chat/useAttachmentController";
import type { useAttachmentReferences } from "../chat/useAttachmentReferences";
interface Options {
  input: string; setInput: (value: string) => void; conversation: Conversation | null; loading: boolean;
  attachmentController: ReturnType<typeof useAttachmentController>; references: ReturnType<typeof useAttachmentReferences>;
}
/** A checkpoint is acknowledged only after the target scope and React draft state are applied. */
export function useUpdateDraft({ input, setInput, conversation, loading, attachmentController, references }: Options) {
  const app = useApp(); const update = useAppUpdate();
  const changes = useRef(0);
  const markChanged = useCallback(() => { changes.current += 1; }, []);
  const snapshot = useRef<RestartDraft | null>(null);
  const busy = useRef(false); busy.current = attachmentController.busy;
  snapshot.current = {
    conversation_id: app.activeConversationId, kind: conversation?.kind ?? app.draft.kind,
    project_id: conversation?.project_id ?? app.draft.projectId,
    chat_group_id: conversation?.chat_group_id ?? app.draft.chatGroupId,
    category: conversation?.resolved_category ?? app.draft.category, text: input,
    attachment_ids: attachmentController.attachments.map((item) => item.id),
    rejected_ids: attachmentController.rejectedAttachments.map((item) => item.receipt_id), references: references.selected,
  };
  useEffect(() => update.registerDraft(() => {
    if (busy.current) throw new Error("draft_failed");
    return snapshot.current;
  }), [update.registerDraft]);
  const phase = useRef<{ id: string; step: "navigation" | "applying" | "acknowledged"; revision: number; changes: number; request: number } | null>(null);
  const lastRequest = useRef(0);
  useEffect(() => {
    const restored = update.restored;
    if (!restored || !app.coreOnline || app.workspaceLoading || attachmentController.busy) return;
    const forced = update.restoreRequest > lastRequest.current;
    let current = phase.current;
    if (!current || current.id !== restored.update_id || forced) {
      const live = snapshot.current;
      if (!forced && (changes.current > 0 || live?.text || live?.attachment_ids.length || live?.rejected_ids.length || live?.references.length)) {
        update.setRestoreConflict(true); return;
      }
      lastRequest.current = update.restoreRequest;
      current = { id: restored.update_id, step: "navigation", revision: app.draft.revision, changes: changes.current, request: update.restoreRequest };
      phase.current = current;
      if (restored.draft.conversation_id && app.conversations.some((item) => item.id === restored.draft.conversation_id)) {
        app.openConversation(restored.draft.conversation_id);
      } else if (restored.draft.kind === "project_work" && restored.draft.project_id && app.projects.some((item) => item.id === restored.draft.project_id)) {
        app.startNewWork(restored.draft.project_id); app.setDraftCategory(restored.draft.category);
      } else {
        const groupId = restored.draft.chat_group_id;
        app.startNewChat(groupId && app.chatGroups.some((group) => group.id === groupId) ? groupId : null);
        app.setDraftCategory(restored.draft.category);
      }
      return;
    }
    if (current.step === "acknowledged") return;
    if (changes.current !== current.changes) { update.setRestoreConflict(true); return; }
    if (current.step === "navigation") {
      const savedConversation = restored.draft.conversation_id && app.conversations.some((item) => item.id === restored.draft.conversation_id);
      if (savedConversation) {
        if (app.activeConversationId !== restored.draft.conversation_id || loading || conversation?.id !== restored.draft.conversation_id) return;
      } else if (app.activeConversationId !== null || app.draft.revision <= current.revision || loading) return;
      setInput(restored.draft.text);
      attachmentController.restore(restored.attachments, restored.rejected_attachments);
      references.restore(restored.draft.references);
      current.step = "applying";
      return;
    }
    if (input !== restored.draft.text || attachmentController.attachments.map((item) => item.id).join() !== restored.attachments.map((item) => item.id).join()) return;
    if (attachmentController.rejectedAttachments.map((item) => item.receipt_id).join() !== restored.rejected_attachments.map((item) => item.receipt_id).join()
      || JSON.stringify(references.selected) !== JSON.stringify(restored.draft.references)) return;
    current.step = "acknowledged";
    void update.finishRestore(restored.update_id);
  });
  return markChanged;
}
