import { ArrowUp, FileText, FileWarning, Image, Paperclip, Square, X } from "lucide-react";
import { useRef, type ClipboardEvent, type FormEvent, type KeyboardEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { AttachmentPreview, MessageAttachment, RejectedAttachment } from "../../types/domain";
import { SkillPicker } from "../abilities/SkillPicker";

interface Props {
  value: string;
  disabled: boolean;
  sendDisabled?: boolean;
  generating?: boolean;
  stopping?: boolean;
  onStop?(): void;
  referenceCount?: number;
  onChange(value: string): void;
  onSend(): void;
  attachments: MessageAttachment[];
  rejectedAttachments: RejectedAttachment[];
  previews: Record<string, AttachmentPreview>;
  attachmentBusy: boolean;
  attachmentError: string | null;
  onAttach(): void;
  onPasteImages(files: File[]): void;
  onRemoveAttachment(id: string): void;
  onRemoveRejectedAttachment(id: string): void;
  webSearchControl?: ReactNode;
}

export function ChatComposer({
  value, disabled, sendDisabled, generating = false, stopping = false, onStop, referenceCount = 0, onChange, onSend, attachments, rejectedAttachments, previews, attachmentBusy,
  attachmentError, onAttach, onPasteImages, onRemoveAttachment, onRemoveRejectedAttachment, webSearchControl,
}: Props) {
  const { t } = useTranslation("chat");
  const composing = useRef(false);
  const canSend = !disabled && !generating && !sendDisabled && !attachmentBusy
    && Boolean(value.trim() || attachments.length || rejectedAttachments.length || referenceCount);
  function submit(event: FormEvent) {
    if (event.defaultPrevented || event.target !== event.currentTarget) return;
    event.preventDefault();
    if (canSend) onSend();
  }
  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !composing.current) {
      event.preventDefault();
      if (canSend) onSend();
    }
  }
  function paste(event: ClipboardEvent<HTMLTextAreaElement>) {
    if (disabled || generating) return;
    const files = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile()).filter((file): file is File => file !== null);
    if (!files.length) files.push(...Array.from(event.clipboardData.files).filter((file) => file.type.startsWith("image/")));
    if (!files.length) return; // Keep the browser's normal text paste behavior.
    event.preventDefault();
    const text = event.clipboardData.getData("text/plain");
    if (text) {
      const editor = event.currentTarget;
      const start = editor.selectionStart;
      const next = (value.slice(0, start) + text + value.slice(editor.selectionEnd)).slice(0, 20000);
      onChange(next);
      const caret = Math.min(start + text.length, next.length);
      requestAnimationFrame(() => editor.setSelectionRange(caret, caret));
    }
    onPasteImages(files);
  }
  return <form className="chat-composer" onSubmit={submit}>
    {attachments.length || rejectedAttachments.length ? <div className="attachment-tray">
      {attachments.map((attachment) => <article key={attachment.id} className="attachment-chip">
        {previews[attachment.id]?.data_url
          ? <img src={previews[attachment.id].data_url as string} alt="" />
          : attachment.kind === "image" ? <Image size={17} /> : <FileText size={17} />}
        <span><strong title={attachment.name}>{attachment.name}</strong>
          <small>{attachment.kind === "image" && attachment.model_width && attachment.model_height
            ? `${attachment.model_width} × ${attachment.model_height} · ${formatBytes(attachment.model_byte_size ?? attachment.byte_size)}`
            : formatBytes(attachment.byte_size)}</small>
          {attachment.media_type === "image/gif" ? <small>{t("attachments.gifFirstFrame")}</small> : null}
        </span>
        <button type="button" aria-label={t("attachments.remove", { name: attachment.name })}
          disabled={generating} onClick={() => onRemoveAttachment(attachment.id)}><X size={13} /></button>
      </article>)}
      {rejectedAttachments.map((attachment) => <article key={attachment.receipt_id}
        className="attachment-chip attachment-chip--rejected">
        <FileWarning size={17} />
        <span><strong title={attachment.display_name}>{attachment.display_name}</strong>
          <small>{t(`attachments.rejection.${attachment.error_code}`, {
            defaultValue: t("attachments.rejection.attachment_processing_failed"),
          })}</small>
          {attachment.detected_media_type ? <small>{attachment.detected_media_type}</small> : null}
        </span>
        <button type="button" aria-label={t("attachments.remove", { name: attachment.display_name })}
          disabled={generating} onClick={() => onRemoveRejectedAttachment(attachment.receipt_id)}><X size={13} /></button>
      </article>)}
    </div> : null}
    {attachmentError ? <p className="attachment-tray__error">{t(`attachments.rejection.${attachmentError}`, {
      defaultValue: attachmentError,
    })}</p> : null}
    <div className="chat-composer__tools"><button type="button" aria-label={t("composer.attach")} title={t("composer.pasteImageHint")} className="chat-composer__icon"
      disabled={disabled || generating || attachmentBusy || attachments.length >= 8} onClick={onAttach}>
      <Paperclip size={19} />
    </button>
    {webSearchControl}
    <SkillPicker disabled={disabled || generating} value={value} onChange={onChange} /></div>
    <textarea value={value} maxLength={20000} rows={1} placeholder={t("composer.placeholder")} disabled={disabled}
      onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }}
      onKeyDown={keyDown} onPaste={paste} onChange={(event) => onChange(event.target.value)} />
    {generating ? <button type="button" aria-label={t(stopping ? "status.stopping" : "status.stop")}
      title={t(stopping ? "status.stopping" : "status.stop")} className="chat-composer__send chat-composer__stop"
      disabled={stopping || !onStop} onClick={(event) => { event.preventDefault(); onStop?.(); }}><Square size={16} fill="currentColor" /></button>
      : <button type="submit" aria-label={t("composer.send")} title={t("composer.send")} className="chat-composer__send"
        disabled={!canSend}><ArrowUp size={20} /></button>}
  </form>;
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}
