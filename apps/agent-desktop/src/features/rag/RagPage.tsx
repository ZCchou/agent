import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { useApp } from "../../contexts/AppContext";
import { ragRepository } from "../../services/runtime";
import type { RagDocumentConfirmation, RagDocumentPreview, RagEvidence, RagJob, RagLibrary, RagLibraryDraft, RagModelList, RagPathSelection, RagProfile } from "../../types/rag";
import { resolveIndexVersions, type IndexVersionChoices } from "./indexVersions";
import "./rag.css";

const profiles: RagProfile[] = ["qwen3-embedding-0.6b", "qwen3-reranker-0.6b", "bge-m3", "bge-reranker-v2-m3"];
type Action = (operation: () => Promise<void>) => Promise<void>;
const emptyModels: RagModelList = { models: [], root: "", runtime: {} };

export function RagPage() {
  const { t } = useTranslation("common");
  const { projects, viewingProjectId, activeConversation, draft } = useApp();
  const [tab, setTab] = useState<"libraries" | "models">("libraries");
  const [models, setModels] = useState<RagModelList>(emptyModels);
  const [libraries, setLibraries] = useState<RagLibrary[]>([]);
  const [jobs, setJobs] = useState<RagJob[]>([]);
  const [projectId, setProjectId] = useState(viewingProjectId ?? activeConversation?.project_id ?? draft.projectId ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [cancellingJobs, setCancellingJobs] = useState<string[]>([]);
  const pending = useRef(false);
  const mounted = useRef(true);
  const refresh = useCallback(async () => {
    const [modelList, libraryList, jobList, bindings] = await Promise.all([ragRepository.listModels(), ragRepository.listLibraries(), ragRepository.listJobs(), projectId ? ragRepository.listLibraries(projectId) : Promise.resolve({ libraries: [] as RagLibrary[] })]);
    if (!mounted.current) return;
    setModels(modelList);
    setLibraries(libraryList.libraries.map((library) => {
      const binding = bindings.libraries.find((item) => item.id === library.id && item.enabled);
      return { ...library, project_ids: binding ? [projectId] : [], bound_index_id: binding?.index_id ?? null };
    }));
    setJobs(jobList.jobs);
  }, [projectId]);
  const act: Action = useCallback(async (operation) => {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError("");
    try { await operation(); } catch (reason) { if (mounted.current) setError(String(reason)); }
    finally { pending.current = false; if (mounted.current) setBusy(false); }
  }, []);
  useEffect(() => { mounted.current = true; void act(refresh); return () => { mounted.current = false; }; }, [act, refresh]);
  useEffect(() => {
    if (!jobs.some((job) => ["queued", "running", "pending"].includes(job.status))) return;
    const timer = window.setInterval(() => { if (!pending.current) void act(refresh); }, 2000);
    return () => window.clearInterval(timer);
  }, [jobs, act, refresh]);
  async function cancelJob(jobId: string) {
    setCancellingJobs((items) => [...items, jobId]);
    try {
      await ragRepository.cancelJob(jobId);
      if (mounted.current) setJobs((items) => items.map((job) => job.id === jobId ? { ...job, status: "cancelled" } : job));
    } catch (reason) {
      if (mounted.current) setError(String(reason));
    } finally {
      if (mounted.current) setCancellingJobs((items) => items.filter((id) => id !== jobId));
    }
  }
  return <div className="settings-shell rag-page">
    <div className="page-heading-row"><div className="page-intro"><h2>{t("rag.title")}</h2><p>{t("rag.description")}</p></div>
      <Button disabled={busy} onClick={() => void act(refresh)}>{t("rag.refresh")}</Button></div>
    <div className="rag-tabs" role="tablist" aria-label={t("rag.title")}>
      {(["libraries", "models"] as const).map((value) => <Button key={value} disabled={busy} role="tab" aria-selected={tab === value} aria-controls={`rag-${value}`} id={`rag-tab-${value}`} onClick={() => setTab(value)}>{t(`rag.${value}`)}</Button>)}
    </div>
    {error && <p role="alert" className="state-message state-message--error">{t("rag.error")} {error}</p>}
    {busy && <p role="status">{t("rag.loading")}</p>}
    <section id={`rag-${tab}`} role="tabpanel" aria-labelledby={`rag-tab-${tab}`}>
      {tab === "models" ? <ModelsPanel models={models} busy={busy} act={act} refresh={refresh} /> : <>
        <label className="rag-field">{t("rag.project")}<select value={projectId} disabled={busy} onChange={(event) => setProjectId(event.target.value)}>
          <option value="">{t("rag.chooseProject")}</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
        </select></label><p>{t("rag.bindingHelp")}</p>
        <LibrariesPanel libraries={libraries} models={models} projectId={projectId} busy={busy} act={act} refresh={refresh} />
        <SearchPanel projectId={projectId} busy={busy} act={act} />
      </>}
    </section>
    <Card className="rag-card"><h3>{t("rag.jobs")}</h3>{!jobs.length && <p>{t("rag.empty")}</p>}
      {jobs.map((job) => <div className="rag-job" key={job.id}><span>{libraries.find((library) => library.id === job.library_id)?.name ?? job.kind} · {t(`rag.statuses.${job.status}`, { defaultValue: job.status })}</span>
        <progress max={1} value={job.progress > 1 ? job.progress / 100 : job.progress} aria-label={t("rag.jobs")} />
        {job.error && <p role="alert">{job.error}</p>}
        {["queued", "running", "pending"].includes(job.status) && <Button disabled={cancellingJobs.includes(job.id)} onClick={() => void cancelJob(job.id)}>{t("rag.cancelJob")}</Button>}
      </div>)}
    </Card>
  </div>;
}

interface PanelProps { busy: boolean; act: Action; refresh: () => Promise<void> }
function ModelsPanel({ models, busy, act, refresh }: PanelProps & { models: RagModelList }) {
  const { t } = useTranslation("common");
  const [profile, setProfile] = useState<RagProfile>(profiles[0]);
  const [device, setDevice] = useState("cpu");
  const [selection, setSelection] = useState<RagPathSelection | null>(null);
  const [inspected, setInspected] = useState(false);
  const [removeId, setRemoveId] = useState<string | null>(null);
  useEffect(() => () => { if (selection) void ragRepository.revokeSelection(selection).catch(() => {}); }, [selection]);
  async function choose(modelFile: boolean) {
    const selected = await ragRepository.selectPaths("model", modelFile);
    if (!selected) return;
    setSelection(selected); setInspected(false);
    const inspection = await ragRepository.inspectModel(selected, profile);
    if (inspection.valid === false) throw new Error(Array.isArray(inspection.errors) ? inspection.errors.join("\n") : t("rag.error"));
    setInspected(true);
  }
  return <div className="rag-stack"><p>{t("rag.modelHelp")}</p>
    <p>{t("rag.runtime")} {t(models.runtime.available ? "rag.statuses.available" : "rag.statuses.unavailable")}</p>
    {typeof models.runtime.error === "string" && <p role="alert">{models.runtime.error}</p>}
    <Card className="rag-card"><h3>{t("rag.root")}</h3><p className="rag-path">{models.root || "—"}</p>
      <Button disabled={busy} onClick={() => void act(async () => { const selected = await ragRepository.selectPaths("root"); if (selected) { await ragRepository.setModelRoot(selected); await refresh(); } })}>{t("rag.chooseRoot")}</Button></Card>
    <Card className="rag-card"><h3>{t("rag.importModel")}</h3><div className="rag-fields">
      <label>{t("rag.modelProfile")}<select value={profile} disabled={busy} onChange={(event) => { setProfile(event.target.value as RagProfile); setInspected(false); setSelection(null); }}>{profiles.map((value) => <option key={value}>{value}</option>)}</select></label>
      <label>{t("rag.device")}<DeviceSelect value={device} disabled={busy} onChange={setDevice} /></label></div>
      <div className="rag-actions"><Button disabled={busy} onClick={() => void act(() => choose(false))}>{t("rag.importFolder")}</Button>
        <Button disabled={busy} onClick={() => void act(() => choose(true))}>{t("rag.importFile")}</Button></div>
      {selection && <><p className="rag-path">{selection.paths[0]}</p><Button disabled={busy} onClick={() => { setSelection(null); setInspected(false); }}>{t("rag.cancel")}</Button></>}
      {inspected && <><p>{t("rag.inspected")}</p><Button variant="primary" disabled={busy} onClick={() => void act(async () => {
        if (!selection) return;
        const selected = selection;
        try { await ragRepository.importModel(selected, profile, device); }
        finally { setSelection(null); setInspected(false); }
        await refresh();
      })}>{t("rag.importModel")}</Button></>}
    </Card>
    {!models.models.length && <p>{t("rag.empty")}</p>}
    {models.models.map((model) => <Card key={model.id} className="rag-card"><h3>{model.name}</h3><p>{model.profile} · {model.backend}</p>
      <p>{t("rag.status")} {t(`rag.statuses.${model.status}`, { defaultValue: model.status })} · {t("rag.dimension")} {model.dimension ?? "—"} · {(model.size_bytes / 1024 ** 3).toFixed(2)} GB</p>
      {model.error && <p role="alert">{model.error}</p>}
      <label className="rag-field">{t("rag.device")}<DeviceSelect value={model.device} disabled={busy} onChange={(value) => void act(async () => { await ragRepository.setModelDevice(model.id, value); await refresh(); })} /></label>
      <div className="rag-actions"><Button disabled={busy} onClick={() => void act(async () => { await ragRepository.probeModel(model.id); await refresh(); })}>{t("rag.probe")}</Button>
        <Button disabled={busy} onClick={() => setRemoveId(model.id)}>{t("rag.remove")}</Button></div>
      {removeId === model.id && <div role="alert"><p>{t("rag.removeConfirm", { name: model.name })}</p><div className="rag-actions"><Button disabled={busy} onClick={() => void act(async () => { await ragRepository.removeModel(model.id); setRemoveId(null); await refresh(); })}>{t("rag.confirm")}</Button><Button onClick={() => setRemoveId(null)}>{t("rag.cancel")}</Button></div></div>}
    </Card>)}
  </div>;
}
function DeviceSelect({ value, disabled, onChange }: { value: string; disabled: boolean; onChange: (value: string) => void }) {
  const { t } = useTranslation("common");
  const devices = ["cpu", "cuda"];
  if (!devices.includes(value)) devices.push(value);
  return <select value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>{devices.map((device) => <option key={device} value={device}>{t(`rag.${device}`, { defaultValue: device })}</option>)}</select>;
}

function LibrariesPanel({ libraries, models, projectId, busy, act, refresh }: PanelProps & { libraries: RagLibrary[]; models: RagModelList; projectId: string }) {
  const { t } = useTranslation("common");
  const [editor, setEditor] = useState<{ id?: string; draft: RagLibraryDraft } | null>(null);
  const [pasteId, setPasteId] = useState<string | null>(null);
  const [pasteTitle, setPasteTitle] = useState("");
  const [pasteContent, setPasteContent] = useState("");
  const [review, setReview] = useState<RagDocumentPreview | null>(null);
  const [removeId, setRemoveId] = useState<string | null>(null);
  const embeddings = models.models.filter((model) => model.role === "embedding");
  const rerankers = models.models.filter((model) => model.role === "reranker");
  return <div className="rag-stack"><p>{t("rag.libraryHelp")}</p>
    <Button disabled={busy || !!editor || !embeddings.length || !rerankers.length} onClick={() => setEditor({ draft: { name: "", embedding_model_id: embeddings[0]?.id ?? "", reranker_model_id: rerankers[0]?.id ?? null } })}>{t("rag.createLibrary")}</Button>
    {!embeddings.length && <p>{t("rag.noEmbedding")}</p>}
    {editor && <Card className="rag-card"><h3>{t(editor.id ? "rag.editLibrary" : "rag.createLibrary")}</h3><div className="rag-fields">
      <label>{t("rag.name")}<input value={editor.draft.name} disabled={busy} maxLength={200} onChange={(event) => setEditor({ ...editor, draft: { ...editor.draft, name: event.target.value } })} /></label>
      <label>{t("rag.embedding")}<select value={editor.draft.embedding_model_id} disabled={busy} onChange={(event) => setEditor({ ...editor, draft: { ...editor.draft, embedding_model_id: event.target.value } })}>{embeddings.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}</select></label>
      <label>{t("rag.reranker")}<select value={editor.draft.reranker_model_id ?? ""} disabled={busy} onChange={(event) => setEditor({ ...editor, draft: { ...editor.draft, reranker_model_id: event.target.value || null } })}>{rerankers.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}</select></label>
    </div><div className="rag-actions"><Button variant="primary" disabled={busy || !editor.draft.name.trim() || !editor.draft.embedding_model_id || !editor.draft.reranker_model_id} onClick={() => void act(async () => { if (editor.id) await ragRepository.updateLibrary(editor.id, editor.draft); else await ragRepository.createLibrary(editor.draft); setEditor(null); await refresh(); })}>{t("rag.save")}</Button><Button disabled={busy} onClick={() => setEditor(null)}>{t("rag.cancel")}</Button></div></Card>}
    {!libraries.length && <p>{t("rag.empty")}</p>}
    {libraries.map((library) => {
      const bound = library.project_id === projectId || !!library.project_ids?.includes(projectId);
      return <Card key={library.id} className="rag-card"><h3>{library.name}</h3><p>{t("rag.embedding")} {models.models.find((model) => model.id === library.embedding_model_id)?.name ?? "—"} · {t("rag.generation")} {library.generation}</p>
        <p className="rag-path">{t("rag.currentIndex")} <code>{library.active_index_id ?? t("rag.noIndex")}</code></p>
        {bound && <p className="rag-path">{t("rag.projectIndex")} <code>{library.bound_index_id ?? t("rag.noIndex")}</code></p>}
        <div className="rag-actions"><Button disabled={busy || !projectId} onClick={() => void act(async () => { await ragRepository.bindLibrary(library.id, projectId, !bound); await refresh(); })}>{t(bound ? "rag.unbind" : "rag.bind")}</Button>
          {bound && <Button disabled={busy || !library.active_index_id || library.bound_index_id === library.active_index_id} onClick={() => void act(async () => { await ragRepository.bindLibrary(library.id, projectId, true); await refresh(); })}>{t("rag.adoptCurrentIndex")}</Button>}
          <Button disabled={busy || !!editor} onClick={() => setEditor({ id: library.id, draft: { name: library.name, embedding_model_id: library.embedding_model_id, reranker_model_id: library.reranker_model_id } })}>{t("rag.editLibrary")}</Button>
          <Button disabled={busy} onClick={() => setRemoveId(library.id)}>{t("rag.remove")}</Button>
          <Button disabled={busy} onClick={() => void act(async () => { const selected = await ragRepository.selectPaths("documents"); if (selected) { await ragRepository.importDocuments(library.id, selected); await refresh(); } })}>{t("rag.importDocuments")}</Button>
          <Button disabled={busy} onClick={() => { setPasteId(library.id); setPasteTitle(""); setPasteContent(""); }}>{t("rag.paste")}</Button></div>
        <IndexVersionPicker library={library} busy={busy} onBuild={(versionIds) => void act(async () => { await ragRepository.buildIndex(library.id, versionIds); await refresh(); })} />
        {removeId === library.id && <div role="alert"><p>{t("rag.removeConfirm", { name: library.name })}</p><div className="rag-actions"><Button disabled={busy} onClick={() => void act(async () => { await ragRepository.removeLibrary(library.id); setRemoveId(null); await refresh(); })}>{t("rag.confirm")}</Button><Button onClick={() => setRemoveId(null)}>{t("rag.cancel")}</Button></div></div>}
        {pasteId === library.id && <div className="rag-stack"><label>{t("rag.titleField")}<input value={pasteTitle} disabled={busy} onChange={(event) => setPasteTitle(event.target.value)} /></label><label>{t("rag.content")}<textarea rows={8} value={pasteContent} disabled={busy} maxLength={2000000} onChange={(event) => setPasteContent(event.target.value)} /></label><div className="rag-actions"><Button disabled={busy || !pasteTitle.trim() || !pasteContent.trim()} onClick={() => void act(async () => { await ragRepository.pasteDocument(library.id, pasteTitle, pasteContent); setPasteId(null); await refresh(); })}>{t("rag.submitPaste")}</Button><Button disabled={busy} onClick={() => setPasteId(null)}>{t("rag.cancel")}</Button></div></div>}
        <h4>{t("rag.documents")}</h4>{!(library.documents ?? []).length && <p>{t("rag.empty")}</p>}
        {(library.documents ?? []).map((document) => <div className="rag-document" key={document.version_id}><div><strong>{document.title}</strong><p>{document.revision || "—"} · {t(`rag.statuses.${document.status}`, { defaultValue: document.status })} · {document.chunk_count} {t("rag.chunks")}</p></div><div className="rag-actions">
          <Button disabled={busy} onClick={() => void act(async () => setReview(await ragRepository.previewDocument(document.version_id)))}>{t("rag.preview")}</Button>
          <Button disabled={busy || document.status === "withdrawn"} onClick={() => void act(async () => { await ragRepository.withdrawDocument(document.version_id); await refresh(); })}>{t("rag.withdraw")}</Button></div></div>)}
      </Card>;
    })}
    {review && <DocumentReview key={review.version.version_id ?? review.version.id} preview={review} busy={busy} onClose={() => setReview(null)} onMore={() => void act(async () => {
      const next = await ragRepository.previewDocument(review.version.version_id ?? review.version.id, review.chunks.length);
      setReview({ ...next, chunks: [...review.chunks, ...next.chunks] });
    })} onConfirm={(confirmation) => void act(async () => { await ragRepository.confirmDocument(confirmation); setReview(null); await refresh(); })} />}
  </div>;
}

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
        {version.revision} · {version.title}
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
    </div><h4>{t("rag.warnings")}</h4>{preview.warnings.length ? <ul>{preview.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul> : <p>{t("rag.noWarnings")}</p>}
    <label className="rag-checkbox"><input type="checkbox" checked={confirmation.warnings_acknowledged} disabled={busy || preview.version.status !== "pending"} onChange={(event) => setConfirmation({ ...confirmation, warnings_acknowledged: event.target.checked })} />{t("rag.acknowledge")}</label>
    <h4>{t("rag.chunks")}</h4><p>{t("rag.untrusted")}</p><div className="rag-passages">{preview.chunks.map((chunk, index) => <article key={chunk.id ?? index}><small>{chunk.clause_number} {formatRagLocator(chunk.locator)}</small><pre>{chunk.text}</pre></article>)}</div>
    {preview.chunks.length < (preview.total_chunks ?? preview.chunks.length) && <Button disabled={busy} onClick={onMore}>{t("rag.loadMore")}</Button>}
    <div className="rag-actions"><Button variant="primary" disabled={busy || !confirmation.title.trim() || !confirmation.revision.trim() || !confirmation.source.trim() || !confirmation.document_number.trim() || !confirmation.warnings_acknowledged || preview.version.status !== "pending"} onClick={() => onConfirm(confirmation)}>{t("rag.confirmDocument")}</Button><Button disabled={busy} onClick={onClose}>{t("rag.close")}</Button></div>
  </section></div>;
}

function SearchPanel({ projectId, busy, act }: { projectId: string; busy: boolean; act: Action }) {
  const { t } = useTranslation("common");
  const [query, setQuery] = useState("");
  const [evidence, setEvidence] = useState<RagEvidence[]>([]);
  const [fullText, setFullText] = useState<{ title: string; text: string } | null>(null);
  useEffect(() => { setEvidence([]); setFullText(null); }, [projectId]);
  return <Card className="rag-card"><h3>{t("rag.search")}</h3><p>{t("rag.searchHelp")}</p><label>{t("rag.query")}<input value={query} onChange={(event) => setQuery(event.target.value)} /></label>
    <Button disabled={busy || !projectId || !query.trim()} onClick={() => void act(async () => { setFullText(null); setEvidence((await ragRepository.search(projectId, query)).evidence); })}>{t("rag.search")}</Button>
    {evidence.map((item) => <article className="rag-evidence" key={item.id}><h4>{item.title} · {item.revision}</h4><p>{item.clause_number} {formatRagLocator(item.locator)}</p><pre>{item.text}</pre><Button disabled={busy} onClick={() => void act(async () => {
      const detail = await ragRepository.previewEvidence(projectId, item.id);
      setFullText({ title: item.title, text: typeof detail.text === "string" ? detail.text : item.text });
    })}>{t("rag.viewEvidence")}</Button></article>)}
    {fullText && <section className="rag-evidence" aria-label={t("rag.evidence")}><h4>{fullText.title}</h4><p>{t("rag.untrusted")}</p><pre>{fullText.text}</pre><Button onClick={() => setFullText(null)}>{t("rag.close")}</Button></section>}
  </Card>;
}

export function formatRagLocator(locator: string | Record<string, unknown> | undefined): string {
  if (typeof locator === "string") return locator;
  if (!locator) return "";
  return Object.values(locator).filter((value) => typeof value === "string" || typeof value === "number").join(" · ");
}
