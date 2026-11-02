import { FileDiff } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import type { ChangeSet } from "../../types/domain";

export function UndoChangeDialog({ open, changeSet, projectName, conversationTitle, busy, onCancel, onConfirm }: {
  open: boolean;
  changeSet: ChangeSet;
  projectName?: string;
  conversationTitle?: string;
  busy: boolean;
  onCancel(): void;
  onConfirm(): void;
}) {
  const { t, i18n } = useTranslation("chat");
  const expires = changeSet.undo_expires_at
    ? new Intl.DateTimeFormat(i18n.language, { dateStyle: "medium", timeStyle: "short" }).format(new Date(changeSet.undo_expires_at))
    : t("changes.undoUnknownExpiry");
  const changedAt = changeSet.created_at
    ? new Intl.DateTimeFormat(i18n.language, { dateStyle: "medium", timeStyle: "short" }).format(new Date(changeSet.created_at))
    : t("changes.undoUnknownExpiry");
  return <ConfirmDialog open={open} title={t("changes.undoDialogTitle")} tone="danger"
    description={t("changes.undoDialogDescription", { project: projectName ?? changeSet.project_id, work: conversationTitle ?? changeSet.conversation_id })}
    confirmLabel={t("changes.undoConfirm")} busy={busy} onCancel={onCancel} onConfirm={onConfirm}
    details={<div className="undo-dialog-details">
      <div><FileDiff size={15} /><strong>{changeSet.title}</strong>
        <span>{t("changes.changedAt", { value: changedAt })}</span><span>{t("changes.undoExpires", { value: expires })}</span></div>
      <ul>{changeSet.mutations.map((mutation, index) => <li key={`${mutation.path}-${index}`}>
        <code>{mutation.kind}</code><span>{mutation.source_path ? `${mutation.source_path} → ` : ""}{mutation.path}</span>
      </li>)}</ul>
      <p>{t("changes.undoOrderWarning")}</p>
      <p>{t("changes.undoNoRedo")}</p>
    </div>} />;
}
