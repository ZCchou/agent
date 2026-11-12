export { RagWorkspace as RagPage } from "./RagWorkspace";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";
import type { RagDocumentConfirmation, RagDocumentPreview, RagLibrary } from "../../types/rag";
import { resolveIndexVersions, type IndexVersionChoices } from "./indexVersions";
import "./rag.css";
export function IndexVersionPicker({ library, busy, onBuild }: { library: RagLibrary; busy: boolean; onBuild: (versionIds: string[]) => void }) {
  const { t } = useTranslation("common");
  const [choices, setChoices] = useState<IndexVersionChoices>({});
  const selection = resolveIndexVersions(library.documents ?? [], choices);
  return <section className="rag-index-versions" aria-label={t("rag.indexVersions")}>
    <h4>{t("rag.indexVersions")}</h4><p>{t("rag.indexVersionsHelp")}</p>
    {Array.from(selection.groups, ([documentId, versions]) => <fieldset key={documentId} disabled={busy}>
      <legend>{versions[0].title}</legend>
      {versions.map((version) => <label className="rag-radio" key={version.version_id}>
        <input type="radio" name={`rag-index-${library.id}-${documentId}`} value={version.version_id}
          checked={selection.versionIds.includes(version.version_id)} onChange={() => setChoices({ ...choices, [documentId]: version.version_id })} />
        {version.revision || "未标注"} · {version.chunk_count} 片段 · {version.ocr_summary?.coverage === "complete" ? "OCR 完成" : version.processing_stage === "initial" ? "初始文字" : "原生文字"} · {version.created_at ? new Date(version.created_at).toLocaleString() : version.title}{version.adopted ? " · 当前采用" : ""}
      </label>)}
      <label className="rag-radio"><input type="radio" name={`rag-index-${library.id}-${documentId}`} value="exclude"
        checked={choices[documentId] === null} onChange={() => setChoices({ ...choices, [documentId]: null })} />{t("rag.excludeDocument")}</label>
    </fieldset>)}
    {!!selection.unresolvedDocumentIds.length && <p role="status">{t("rag.chooseRevisionRequired")}</p>}
    <Button disabled={busy || !selection.versionIds.length || !!selection.unresolvedDocumentIds.length} onClick={() => onBuild(selection.versionIds)}>{t("rag.index")}</Button>
  </section>;
}

export function DocumentReview({ preview, busy, onClose, onMore, onConfirm }: { preview: RagDocumentPreview; busy: boolean; onClose: () => void; onMore: () => void; onConfirm: (confirmation: RagDocumentConfirmation) => void }) {
  const { t } = useTranslation("common");
  const dialog = useRef<HTMLElement>(null);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.current?.focus();
    return () => previous?.focus();
  }, []);
  const [confirmation, setConfirmation] = useState<RagDocumentConfirmation>({ version_id: preview.version.version_id ?? preview.version.id, title: preview.version.title ?? "", revision: preview.version.revision ?? "", source: preview.version.source ?? "", document_number: preview.version.document_number ?? "", warnings_acknowledged: false });
  return <div className="rag-modal-backdrop"><section className="rag-modal" role="dialog" aria-modal="true" aria-labelledby="rag-review-title" tabIndex={-1} ref={dialog} onKeyDown={(event) => {
    if (event.key === "Escape" && !busy) onClose();
    if (event.key === "Tab") {
      const controls = Array.from(dialog.current?.querySelectorAll<HTMLElement>('input:not(:disabled), button:not(:disabled), select:not(:disabled), textarea:not(:disabled)') ?? []);
      const last = controls[controls.length - 1];
      const target = event.shiftKey ? last : controls[0];
      if ((event.shiftKey && document.activeElement === controls[0]) || (!event.shiftKey && document.activeElement === last)) { event.preventDefault(); target?.focus(); }
    }
  }}>
    <h3 id="rag-review-title">{t("rag.preview")}</h3><p>{t("rag.reviewHelp")}</p><div className="rag-fields">
      {(["title", "revision", "source", "document_number"] as const).map((field) => <label key={field}>{t(`rag.${field === "title" ? "titleField" : field === "document_number" ? "documentNumber" : field}`)}<input value={confirmation[field]} disabled={busy || preview.version.status !== "pending"} maxLength={500} onChange={(event) => setConfirmation({ ...confirmation, [field]: event.target.value })} /></label>)}
    </div><h4>{t("rag.warnings")}</h4>{preview.warnings.length ? <ul>{preview.warnings.map((warning, index) => <li key={index}>{warning === "OCR is running. Image regions are not yet included." ? t("rag.ocrInitialSnapshot") : warning}</li>)}</ul> : <p>{t("rag.noWarnings")}</p>}
    <label className="rag-checkbox"><input type="checkbox" checked={confirmation.warnings_acknowledged} disabled={busy || preview.version.status !== "pending"} onChange={(event) => setConfirmation({ ...confirmation, warnings_acknowledged: event.target.checked })} />{t("rag.acknowledge")}</label>
    <h4>{t("rag.chunks")}</h4><p>{t("rag.untrusted")}</p><div className="rag-passages">{preview.chunks.map((chunk, index) => <article key={chunk.id ?? index}><small>{chunk.clause_number} {formatRagLocator(chunk.locator)}</small><pre>{chunk.text}</pre></article>)}</div>
    {preview.chunks.length < (preview.total_chunks ?? preview.chunks.length) && <Button disabled={busy} onClick={onMore}>{t("rag.loadMore")}</Button>}
    <div className="rag-actions"><Button variant="primary" disabled={busy || !confirmation.title.trim() || !confirmation.revision.trim() || !confirmation.source.trim() || !confirmation.document_number.trim() || !confirmation.warnings_acknowledged || preview.version.status !== "pending"} onClick={() => onConfirm(confirmation)}>{t("rag.confirmDocument")}</Button><Button disabled={busy} onClick={onClose}>{t("rag.close")}</Button></div>
  </section></div>;
}

export function formatRagLocator(locator: string | Record<string, unknown> | undefined): string {
  if (typeof locator === "string") return locator;
  if (!locator) return "";
  return Object.values(locator).filter((value) => typeof value === "string" || typeof value === "number").join(" · ");
}
