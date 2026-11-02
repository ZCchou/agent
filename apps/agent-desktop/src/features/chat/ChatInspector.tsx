import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ChatInspectorEntry } from "../../contexts/ChatInspectorContext";
import { ContextDetails } from "../context-inspector/ContextDetails";

interface Props {
  entry: ChatInspectorEntry;
  onClose(): void;
  retrying?: boolean;
  onExpandedRetry?(): Promise<void> | void;
  onReferenceAttachment?(id: string, range: { source_start: number; source_end: number }): void;
  referenceDisabled?: boolean;
}

export function ChatInspector({ entry, onClose, retrying, onExpandedRetry, onReferenceAttachment, referenceDisabled }: Props) {
  const { t } = useTranslation("chat");
  const closeButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!entry.open) return;
    closeButton.current?.focus();
    const keyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", keyDown);
    return () => window.removeEventListener("keydown", keyDown);
  }, [entry.open, onClose]);

  if (!entry.open || !entry.view) return null;
  const title = entry.view.kind === "context"
    ? t("context.details")
    : entry.view.title;

  return <>
    <button className="chat-inspector-backdrop" aria-label={t("context.close")} onClick={onClose} />
    <aside className="chat-inspector" role="dialog" aria-modal="false" aria-label={title}>
      <header>
        <h2>{title}</h2>
        <button ref={closeButton} type="button" aria-label={t("context.close")} onClick={onClose}><X size={18} /></button>
      </header>
      <div className="chat-inspector__content">
        {entry.view.kind === "context" ? <ContextDetails runId={entry.view.runId}
          snapshot={entry.view.snapshot} loading={entry.view.loading} error={entry.view.error}
          retrying={retrying} onExpandedRetry={onExpandedRetry} /> : null}
        {entry.view.kind === "project_file" ? entry.view.dataUrl
          ? <img className="chat-inspector__image" src={entry.view.dataUrl} alt={entry.view.title} />
          : <ProjectFileContent content={entry.view.content ?? ""} lineStart={entry.view.lineStart} lineEnd={entry.view.lineEnd} /> : null}
        {entry.view.kind === "attachment" ? entry.view.dataUrl
          ? <img className="chat-inspector__image" src={entry.view.dataUrl} alt={entry.view.title} />
          : <AttachmentTextContent key={entry.view.attachmentId} attachmentId={entry.view.attachmentId}
            content={entry.view.content} onReference={onReferenceAttachment} referenceDisabled={referenceDisabled} /> : null}
      </div>
    </aside>
  </>;
}

function AttachmentTextContent({ attachmentId, content, onReference, referenceDisabled }: {
  attachmentId: string;
  content: string | null;
  onReference?: Props["onReferenceAttachment"];
  referenceDisabled?: boolean;
}) {
  const { t } = useTranslation("chat");
  const preview = useRef<HTMLPreElement>(null);
  const [range, setRange] = useState<{ source_start: number; source_end: number } | null>(null);
  useEffect(() => {
    setRange(null);
    const update = () => {
      const element = preview.current;
      const selection = window.getSelection();
      if (!element || !selection || selection.rangeCount !== 1 || selection.isCollapsed) {
        setRange(null);
        return;
      }
      const selected = selection.getRangeAt(0);
      if (!element.contains(selected.startContainer) || !element.contains(selected.endContainer)) {
        setRange(null);
        return;
      }
      const prefix = document.createRange();
      prefix.selectNodeContents(element);
      prefix.setEnd(selected.startContainer, selected.startOffset);
      const source_start = Array.from(prefix.toString()).length;
      const source_end = source_start + Array.from(selected.toString()).length;
      setRange(source_end > source_start ? { source_start, source_end } : null);
    };
    document.addEventListener("selectionchange", update);
    return () => document.removeEventListener("selectionchange", update);
  }, [content]);
  return <>
    {content !== null && onReference ? <button type="button" disabled={!range || referenceDisabled}
      onMouseDown={(event) => event.preventDefault()}
      onClick={() => { if (range) onReference(attachmentId, range); }}>
      {t("attachments.referenceSelection")}
    </button> : null}
    <pre ref={preview} className="chat-inspector__file">{content ?? t("attachments.previewUnavailable")}</pre>
  </>;
}

function ProjectFileContent({ content, lineStart, lineEnd }: { content: string; lineStart: number | null; lineEnd: number | null }) {
  const lines = content.split("\n").slice(0, 5000);
  return <pre className="chat-inspector__file" aria-label="Project file preview">{lines.map((line, index) => {
    const number = index + 1;
    const highlighted = lineStart !== null && number >= lineStart && number <= (lineEnd ?? lineStart);
    return <span className={highlighted ? "chat-inspector__file-line chat-inspector__file-line--highlight" : "chat-inspector__file-line"} key={number}>
      <span aria-hidden="true">{number}</span><code>{line || " "}</code>{"\n"}
    </span>;
  })}</pre>;
}
