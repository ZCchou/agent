import { memo, useState } from "react";
import { FileText, FileWarning, Image, Pencil } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { ChatMessage, MessageAttachment } from "../../types/domain";
import { AssistantMessageBody } from "./markdown/AssistantMessageBody";
import type { ProjectDirectoryTarget, ProjectFileTarget } from "./markdown/markdownPolicy";
import { parseToolEvent } from "./timelineModel";
import { SearchAuditLabel } from "./SearchAuditLabel";

export const MessageTimelineItem = memo(function MessageTimelineItem({ message, onProjectFile, onProjectDirectory, onAttachment, onReferenceAttachment, referenceDisabled, onBeginEdit, onEdit, editDisabled }: {
  message: ChatMessage;
  onProjectFile?: (target: ProjectFileTarget) => void;
  onProjectDirectory?: (target: ProjectDirectoryTarget) => void;
  onAttachment?: (attachment: MessageAttachment) => void;
  onReferenceAttachment?: (attachment: MessageAttachment) => void;
  referenceDisabled?: boolean;
  onBeginEdit?(): Promise<boolean>;
  onEdit?(message: ChatMessage, content: string): Promise<boolean | undefined>;
  editDisabled?: boolean;
}) {
  const { t } = useTranslation("chat");
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState(message.content);
  const [editBusy, setEditBusy] = useState(false);
  async function beginEdit() {
    setEditBusy(true);
    try {
      if (await onBeginEdit?.()) { setEditValue(message.content); setEditing(true); }
    } finally { setEditBusy(false); }
  }
  async function saveEdit() {
    if (editBusy || editDisabled || (!editValue.trim() && !message.attachments?.length)) return;
    setEditBusy(true);
    try { if (await onEdit?.(message, editValue)) setEditing(false); }
    finally { setEditBusy(false); }
  }
  const operation = parseToolEvent(message);
  return <article className={`chat-message chat-message--${message.role}${message.source_message_id ? " chat-message--derived" : ""}`}>
    <span>{message.role === "user" ? t("roles.you") : message.role === "system_event" ? t("roles.system") : t("roles.agent")}</span>
    {message.role === "assistant" ? (
      <AssistantMessageBody content={message.content} state="complete" onProjectFile={onProjectFile} onProjectDirectory={onProjectDirectory} />
    ) : (
      <div className="chat-message__bubble chat-message__bubble--plain">{editing ? <div className="message-editor">
        <textarea autoFocus aria-label={t("messages.editQuestion")} value={editValue} maxLength={20000}
          disabled={editBusy} onChange={(event) => setEditValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape" && !editBusy) { event.preventDefault(); setEditing(false); }
            if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void saveEdit(); }
          }} />
        <p className="message-editor__hint">{t("messages.editHint")}</p>
        <div className="message-editor__actions">
          <button type="button" disabled={editBusy} onClick={() => setEditing(false)}>{t("messages.cancelEdit")}</button>
          <button type="button" disabled={editBusy || editDisabled || (!editValue.trim() && !message.attachments?.length)}
            onClick={() => void saveEdit()}>{t("messages.saveEdit")}</button>
        </div>
      </div> : <p>{operation
        ? t("tools.timelineEvent", { tool: operation.tool_name, status: t(`tools.operationStatus.${operation.status}`), bytes: operation.output_bytes })
        : message.content}</p>}
        {operation?.tool_name === "fs.search" ? <SearchAuditLabel value={operation.search_completeness} /> : null}
        {message.attachments?.length ? <div className="message-attachments">
          {message.attachments.map((attachment) => <span className="message-attachment-actions" key={attachment.id}><button type="button" title={attachment.name} onClick={() => onAttachment?.(attachment)}>
            {attachment.kind === "image" ? <Image size={13} /> : <FileText size={13} />}<span>{attachment.name}</span>
          </button>{onReferenceAttachment ? <button type="button" disabled={referenceDisabled} aria-label={t("attachments.referenceNamed", { name: attachment.name })}
            onClick={() => onReferenceAttachment(attachment)}>{t("attachments.reference")}</button> : null}</span>)}
        </div> : null}
        {message.attachment_rejections?.length ? <div className="message-attachments message-attachments--rejected"
          aria-label={t("attachments.unreadSummary", { count: message.attachment_rejections.length })}>
          {message.attachment_rejections.map((attachment) => <span key={attachment.id}
            title={t(`attachments.rejection.${attachment.error_code}`, {
              defaultValue: t("attachments.rejection.attachment_processing_failed"),
            })}>
            <FileWarning size={13} /><span>{attachment.display_name}</span>
          </span>)}
        </div> : null}
      </div>
    )}
    {message.role === "user" && onEdit && !editing ? <div className="user-message-actions">
      <button type="button" aria-label={t("messages.edit")} title={t("messages.edit")}
        disabled={editBusy || message.id.startsWith("pending-")} onClick={() => void beginEdit()}><Pencil size={14} /></button>
    </div> : null}
  </article>;
});
