import { FileText, Image, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { ChatMessage, MessageAttachment } from "../../types/domain";
import type { SelectedAttachmentReference } from "./useAttachmentReferences";

interface Props {
  selected: SelectedAttachmentReference[];
  candidates: MessageAttachment[];
  messages: ChatMessage[];
  requiresChoice: boolean;
  disabled: boolean;
  onChoose(attachment: MessageAttachment): void;
  onRemove(id: string): void;
  onDismissCandidates(): void;
}

export function AttachmentReferenceTray(props: Props) {
  const { t } = useTranslation("chat");
  return <>
    {props.selected.length ? <section className="attachment-reference-tray" aria-label={t("attachments.references")}>
      <strong>{t("attachments.references")}</strong>
      <div className="attachment-tray">{props.selected.map(({ attachment, reference }) => <article className="attachment-chip" key={attachment.id}>
        {attachment.kind === "image" ? <Image size={17} /> : <FileText size={17} />}
        <span><strong title={attachment.name}>{attachment.name}</strong>
          <small>{reference.source_start !== undefined && reference.source_end !== undefined
            ? t("attachments.referenceRange", { start: reference.source_start, end: reference.source_end })
            : t("attachments.fromHistory")}</small></span>
        <button type="button" disabled={props.disabled} aria-label={t("attachments.removeReference", { name: attachment.name })}
          onClick={() => props.onRemove(attachment.id)}><X size={13} /></button>
      </article>)}</div>
    </section> : null}
    {props.candidates.length ? <section className="attachment-reference-choices" aria-label={t("attachments.chooseReferences")}>
      {props.requiresChoice ? <p role="status">{t("attachments.chooseBeforeSending")}</p> : null}
      {props.candidates.map((attachment) => {
        const source = props.messages.find((message) => message.attachments?.some((item) => item.id === attachment.id));
        return <div key={attachment.id}>
          <button type="button" disabled={props.disabled} onClick={() => props.onChoose(attachment)}
            aria-label={t("attachments.referenceNamed", { name: attachment.name })}>{attachment.name}</button>
          <small>{source?.content.slice(0, 100) || (source?.created_at ? new Date(source.created_at).toLocaleString() : t("attachments.fromHistory"))}</small>
        </div>;
      })}
      <button type="button" disabled={props.disabled} onClick={props.onDismissCandidates}>{t("attachments.dismissCandidates")}</button>
    </section> : null}
  </>;
}
