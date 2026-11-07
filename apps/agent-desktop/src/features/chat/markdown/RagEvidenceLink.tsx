import { BookOpen } from "lucide-react";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { useApp } from "../../../contexts/AppContext";
import { ragRepository } from "../../../services/runtime";
import "./RagEvidenceLink.css";

const PAGE_LENGTH = 8000;
type EvidencePage = Record<string, unknown>;

export async function loadRagEvidencePage(projectId: string, evidenceId: string, offset: number, signal: AbortSignal): Promise<EvidencePage | null> {
  if (signal.aborted) return null;
  const result = await ragRepository.previewEvidence(projectId, evidenceId, offset, PAGE_LENGTH);
  return signal.aborted ? null : result;
}

export function RagEvidenceLink({ evidenceId, children }: { evidenceId: string; children: ReactNode }) {
  const { t } = useTranslation("common");
  const { activeConversation } = useApp();
  const projectId = activeConversation?.project_id ?? null;
  const [selection, setSelection] = useState<{ projectId: string | null; evidenceId: string; offset: number } | null>(null);
  const [page, setPage] = useState<EvidencePage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { setSelection(null); }, [projectId, evidenceId]);
  useEffect(() => {
    if (!selection || selection.projectId !== projectId || selection.evidenceId !== evidenceId) return;
    if (!projectId) { setLoading(false); setError("rag.noProject"); return; }
    const controller = new AbortController();
    void loadRagEvidencePage(projectId, evidenceId, selection.offset, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setPage(result);
      setLoading(false);
    }).catch((reason: unknown) => {
      if (controller.signal.aborted) return;
      const detail = reason instanceof Error ? reason.message : typeof reason === "string" ? reason : JSON.stringify(reason);
      setError(/rag_evidence_invalid|withdrawn|unauthorized|forbidden|denied/i.test(detail ?? "") ? "rag.evidenceUnavailable" : "rag.evidenceError");
      setLoading(false);
    });
    return () => controller.abort();
  }, [selection, projectId, evidenceId]);

  function selectPage(offset: number) {
    setPage(null);
    setError(null);
    setLoading(true);
    setSelection({ projectId, evidenceId, offset });
  }

  const open = selection && selection.projectId === projectId && selection.evidenceId === evidenceId;
  return <>
    <button type="button" className="markdown-link markdown-link--evidence" title={t("rag.viewEvidence")} aria-haspopup="dialog" onClick={() => selectPage(0)}>
      <span>{children}</span><BookOpen size={12} aria-hidden="true" />
    </button>
    {open && createPortal(<RagEvidenceDialog page={page} loading={loading} error={error} onClose={() => setSelection(null)} onPage={selectPage} />, document.body)}
  </>;
}

function metadataText(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

export function RagEvidenceDialog({ page, loading, error, onClose, onPage }: {
  page: EvidencePage | null; loading: boolean; error: string | null;
  onClose: () => void; onPage: (offset: number) => void;
}) {
  const { t } = useTranslation("common");
  const titleId = useId();
  const dialog = useRef<HTMLElement>(null);
  const contextValue = page?.parent_context;
  const parentContext = contextValue && typeof contextValue === "object" && !Array.isArray(contextValue)
    ? contextValue as Record<string, unknown> : null;
  const offset = typeof page?.offset === "number" ? page.offset : 0;
  const total = Math.max(typeof page?.total_length === "number" ? page.total_length : 0,
    typeof parentContext?.total_length === "number" ? parentContext.total_length : 0);
  const contextScope = metadataText(parentContext?.scope);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.current?.focus();
    return () => previous?.focus();
  }, []);

  return <div className="chat-evidence-backdrop"><section className="chat-evidence-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-busy={loading} tabIndex={-1} ref={dialog} onKeyDown={(event) => {
    if (event.key === "Escape") onClose();
    if (event.key === "Tab") {
      const controls = Array.from(dialog.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? []);
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog.current)) { event.preventDefault(); first?.focus(); }
    }
  }}>
    <h3 id={titleId}>{t("rag.evidence")}</h3>
    {loading && <p role="status">{t("rag.loading")}</p>}
    {error && <p role="alert">{t(error)}</p>}
    {page && <>
      <h4>{metadataText(page.title)}</h4>
      <dl>
        <dt>{t("rag.revision")}</dt><dd>{metadataText(page.revision)}</dd>
        <dt>{t("rag.evidenceClause")}</dt><dd>{metadataText(page.clause_number)}</dd>
        <dt>{t("rag.evidenceLocation")}</dt><dd><pre>{typeof page.locator === "object" && page.locator !== null ? JSON.stringify(page.locator, null, 2) : metadataText(page.locator)}</pre></dd>
      </dl>
      <p>{t("rag.untrusted")}</p>
      <h4>{t("rag.evidenceFragment")}</h4>
      <pre className="chat-evidence-text">{metadataText(page.text)}</pre>
      {parentContext && <>
        <h4>{t("rag.evidenceParentContext")}</h4>
        {contextScope && <p>{t(`rag.evidenceContextScopes.${contextScope}`, { defaultValue: contextScope })}</p>}
        <pre className="chat-evidence-text">{metadataText(parentContext.text)}</pre>
      </>}
      <p>{t("rag.evidenceRange", { start: total ? offset + 1 : 0, end: Math.min(offset + PAGE_LENGTH, total), total })}</p>
      <div className="chat-evidence-actions">
        <button type="button" disabled={loading || offset <= 0} onClick={() => onPage(Math.max(0, offset - PAGE_LENGTH))}>{t("rag.evidencePrevious")}</button>
        <button type="button" disabled={loading || offset + PAGE_LENGTH >= total} onClick={() => onPage(offset + PAGE_LENGTH)}>{t("rag.evidenceNext")}</button>
      </div>
    </>}
    <div className="chat-evidence-actions"><button type="button" onClick={onClose}>{t("rag.close")}</button></div>
  </section></div>;
}
