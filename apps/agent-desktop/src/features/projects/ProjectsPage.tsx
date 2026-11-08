import { isLocalProvider } from "../../services/modelSelection";
import { Archive, BriefcaseBusiness, Folder, FolderKey, MoreHorizontal, Pencil, Pin, Plus, X } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";
import { EmptyState } from "../../components/ui/EmptyState";
import { useApp } from "../../contexts/AppContext";
import { modelRunRepository, projectRepository, settingsRepository, workToolRepository } from "../../services/runtime";
import {
  PROJECT_CATEGORIES, type ProjectCategory, type ProjectGrant, type ProjectKnowledge,
  type ProjectModelRef, type ProjectModelScope,
} from "../../types/domain";
import type { AppSettings } from "../../types/settings";
import { useResourceActions } from "../resources/ResourceActionsProvider";
import { useScopedAction } from "../chat/useScopedAction";


export function ProjectsPage() {
  const { t } = useTranslation("common");
  const resourceActions = useResourceActions();
  const {
    projects, conversations, workspaceLoading, workspaceError, refreshWorkspace,
    viewingProjectId, setCurrentPage, openProject, showProjects, openConversation, startNewWork,
    newProjectRequested, consumeNewProjectRequest,
  } = useApp();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [category, setCategory] = useState<ProjectCategory>("work");
  const [chooseDirectoryOnCreate, setChooseDirectoryOnCreate] = useState(true);
  const creation = useScopedAction("create-project", t("buttonAudit.actionFailed"));
  const saving = creation.busy;
  const [createdProjectId, setCreatedProjectId] = useState<string | null>(null);
  const createdProjectRef = useRef<string | null>(null);
  const directoryChosenRef = useRef(false);
  const [grant, setGrant] = useState<ProjectGrant | null>(null);
  const [knowledge, setKnowledge] = useState<ProjectKnowledge[]>([]);
  const [appSettings, setAppSettings] = useState<AppSettings | null>(null);
  const [modelScope, setModelScope] = useState<ProjectModelScope | null>(null);
  const selected = projects.find((project) => project.id === viewingProjectId) ?? null;
  const action = useScopedAction(selected?.id ?? "projects", t("buttonAudit.actionFailed"));
  const selectedRef = useRef({ id: selected?.id, revision: 0 });
  if (selectedRef.current.id !== selected?.id) selectedRef.current = { id: selected?.id, revision: selectedRef.current.revision + 1 };
  const scopeRef = useRef<ProjectModelScope | null>(null);
  const confirmedScopeRef = useRef<ProjectModelScope | null>(null);
  const scopeQueue = useRef(Promise.resolve());
  const mountedRef = useRef(true);
  useEffect(() => { mountedRef.current = true; return () => { mountedRef.current = false; }; }, []);
  const scopeRevision = useRef(0);
  const [scopeBusy, setScopeBusy] = useState(false);
  const [scopeError, setScopeError] = useState<string | null>(null);
  const [loadRevision, setLoadRevision] = useState(0);
  const works = conversations.filter(
    (conversation) => conversation.kind === "project_work" && conversation.project_id === selected?.id,
  );

  useEffect(() => {
    if (!newProjectRequested) return;
    setOpen(true);
    consumeNewProjectRequest();
  }, [consumeNewProjectRequest, newProjectRequested]);

  useEffect(() => {
    let active = true;
    scopeRevision.current += 1;
    scopeRef.current = null; confirmedScopeRef.current = null;
    setGrant(null); setKnowledge([]); setModelScope(null); setAppSettings(null);
    setScopeBusy(false); setScopeError(null);
    if (!selected) {
      return;
    }
    const failed = () => { if (active) setScopeError(t("buttonAudit.actionFailed")); };
    void workToolRepository.getProjectGrant(selected.id).then(value => { if (active) setGrant(value); }).catch(failed);
    void modelRunRepository.listKnowledge(selected.id).then(value => { if (active) setKnowledge(value); }).catch(failed);
    void settingsRepository.getAppSettings().then(value => { if (active) setAppSettings(value); }).catch(failed);
    void projectRepository.getModelScope(selected.id).then(value => {
      if (active) { scopeRef.current = value; confirmedScopeRef.current = value; setModelScope(value); }
    }).catch(failed);
    return () => { active = false; };
  }, [selected?.id, loadRevision]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) return;
    await creation.run(async isCurrent => {
      let projectId = createdProjectRef.current;
      if (!projectId) {
        const project = await projectRepository.create(name.trim(), category);
        if (!isCurrent()) return;
        projectId = project.id; createdProjectRef.current = project.id; setCreatedProjectId(project.id);
        await refreshWorkspace();
        if (!isCurrent()) return;
      }
      if (chooseDirectoryOnCreate && !directoryChosenRef.current) {
        await workToolRepository.chooseProjectDirectory(projectId);
        if (!isCurrent()) return;
        directoryChosenRef.current = true;
      }
      await refreshWorkspace();
      if (!isCurrent()) return;
      openProject(projectId);
      setName("");
      setCategory("work");
      createdProjectRef.current = null; setCreatedProjectId(null); directoryChosenRef.current = false;
      setOpen(false);
    });
  }

  async function changeCategory(next: ProjectCategory) {
    if (!selected) return;
    await action.run(async () => { await projectRepository.update(selected.id, selected.name, next); await refreshWorkspace(); });
  }

  async function chooseDirectory() {
    if (!selected) return;
    await action.run(async isCurrent => {
      const next = await workToolRepository.chooseProjectDirectory(selected.id);
      if (next && isCurrent()) setGrant(next);
    });
  }

  async function changeModelPolicy(policy: "local_only" | "allow_online") {
    if (!selected) return;
    await action.run(async () => { await projectRepository.updateModelPolicy(selected.id, policy); await refreshWorkspace(); });
  }

  async function changeWebAccess(policy: "disabled" | "allow_search") {
    if (!selected) return;
    await action.run(async () => { await projectRepository.updateWebAccess(selected.id, policy); await refreshWorkspace(); });
  }

  async function changeModelScopePolicy(policy: "inherit_global" | "restricted") {
    if (!selected || !appSettings) return;
    const currentlyAllowed = appSettings.models.providers.flatMap((provider) =>
      provider.enabled ? provider.models.filter((model) => model.enabled && (model.status !== "unavailable" || provider.id.startsWith("managed:")) && model.capabilities.text !== "no" && (
        policy !== "restricted" || selected.model_access_policy !== "local_only" || isLocalProvider(provider)
      )).map((model) => ({ provider_profile_id: provider.id, model_id: model.id })) : [],
    );
    await action.run(async isCurrent => {
      const next = await projectRepository.updateModelScope(
        selected.id, policy, policy === "restricted" ? (scopeRef.current?.models.length ? scopeRef.current.models : currentlyAllowed) : [],
      );
      if (isCurrent()) { scopeRef.current = next; confirmedScopeRef.current = next; setModelScope(next); }
      await refreshWorkspace();
    });
  }

  async function toggleProjectModel(model: ProjectModelRef, enabled: boolean) {
    const current = scopeRef.current;
    if (!selected || !current || current.policy !== "restricted") return;
    const next = enabled
      ? [...current.models, model]
      : current.models.filter((item) => item.provider_profile_id !== model.provider_profile_id || item.model_id !== model.model_id);
    if (next.length === 0) return;
    const projectId = selected.id;
    const projectRevision = selectedRef.current.revision;
    const revision = ++scopeRevision.current;
    const optimistic = { ...current, models: next };
    scopeRef.current = optimistic; setModelScope(optimistic); setScopeBusy(true); setScopeError(null);
    const isCurrent = () => mountedRef.current && selectedRef.current.id === projectId && selectedRef.current.revision === projectRevision;
    scopeQueue.current = scopeQueue.current.then(async () => {
      try {
        const saved = await projectRepository.updateModelScope(projectId, "restricted", next);
        if (isCurrent()) {
          confirmedScopeRef.current = saved;
          if (scopeRevision.current === revision) { scopeRef.current = saved; setModelScope(saved); setScopeError(null); }
        }
      } catch {
        if (isCurrent()) {
          setScopeError(t("buttonAudit.actionFailed"));
          if (scopeRevision.current === revision) { scopeRef.current = confirmedScopeRef.current; setModelScope(confirmedScopeRef.current); }
        }
      } finally { if (isCurrent() && scopeRevision.current === revision) setScopeBusy(false); }
    });
    await scopeQueue.current;
  }

  async function editKnowledge(item: ProjectKnowledge) {
    const title = window.prompt(t("models.knowledgeTitle"), item.title)?.trim();
    if (!title) return;
    const content = window.prompt(t("models.knowledgeContent"), item.content)?.trim();
    if (!content) return;
    await updateKnowledge(() => modelRunRepository.updateKnowledge(item.id, title, content));
  }

  async function updateKnowledge(operation: () => Promise<unknown>) {
    if (!selected) return;
    await action.run(async isCurrent => {
      await operation();
      const next = await modelRunRepository.listKnowledge(selected.id);
      if (isCurrent()) setKnowledge(next);
    });
  }

  return (
    <div className="projects-page">
      <div className="page-heading-row">
        <div className="page-intro">
          {selected ? <button className="text-action" onClick={showProjects}>{t("projects.detail.back")}</button> : null}
          <h2 {...(selected ? resourceActions.menuHandlers({ kind: "project", id: selected.id }) : {})}>{selected?.name ?? t("pages.projects.title")}</h2>
          <p>{selected ? t("projects.detail.description") : t("pages.projects.description")}</p>
        </div>
        <div className="page-heading-actions">
          {selected && <>
            <Button icon={<Pencil size={16} />} onClick={() => resourceActions.rename({ kind: "project", id: selected.id })}>{t("actions.rename")}</Button>
            <Button icon={<MoreHorizontal size={16} />} aria-label={t("sidebar.moreActions")}
              onClick={(event) => resourceActions.openMenu({ kind: "project", id: selected.id }, event)} />
          </>}
          <Button variant="primary" icon={<Plus size={17} />} onClick={() => setOpen(true)}>{t("projects.new")}</Button>
        </div>
      </div>

      {workspaceLoading ? <p className="state-message">{t("projects.loading")}</p> : null}
      {workspaceError ? (
        <div className="state-message state-message--error">
          {t("projects.error")} <button onClick={() => void refreshWorkspace()}>{t("actions.retry")}</button>
        </div>
      ) : null}
      {action.error || scopeError ? <p className="form-error" role="alert">{action.error ?? scopeError}
        {scopeError ? <button disabled={scopeBusy || action.busy} onClick={() => setLoadRevision(value => value + 1)}>{t("actions.retry")}</button> : null}
      </p> : null}

      {selected ? (
        <section className="project-detail">
          <div className="project-detail__settings">
            <label>{t("projects.drawer.category")}
              <select value={selected.category} disabled={action.busy || scopeBusy} onChange={(event) => void changeCategory(event.target.value as ProjectCategory)}>
                {PROJECT_CATEGORIES.map((item) => (
                  <option key={item} value={item}>{t(`projects.categories.${item}`)}</option>
                ))}
              </select>
            </label>
            <label>{t("models.projectPolicy")}
              <select value={selected.model_access_policy ?? "local_only"}
                disabled={action.busy || scopeBusy}
                onChange={(event) => void changeModelPolicy(event.target.value as "local_only" | "allow_online")}>
                <option value="local_only">{t("models.localOnly")}</option>
                <option value="allow_online">{t("models.allowOnline")}</option>
              </select>
            </label>
            <label>{t("models.projectModelScope")}
              <select value={modelScope?.policy ?? "inherit_global"}
                disabled={action.busy || scopeBusy || !modelScope || !appSettings}
                onChange={(event) => void changeModelScopePolicy(event.target.value as "inherit_global" | "restricted")}>
                <option value="inherit_global">{t("models.inheritGlobalModels")}</option>
                <option value="restricted">{t("models.restrictProjectModels")}</option>
              </select>
            </label>
            <label>{t("settings.webSearch.projectAccess")}
              <select value={selected.web_access_policy ?? "disabled"}
                disabled title="离线模式已停用联网检索"
                onChange={(event) => void changeWebAccess(event.target.value as "disabled" | "allow_search")}>
                <option value="disabled">{t("settings.webSearch.projectOff")}</option>
                <option value="allow_search">{t("settings.webSearch.projectOn")}</option>
              </select>
              <small>{t("settings.webSearch.projectAccessHint")}</small>
            </label>
            {modelScope?.policy === "restricted" && appSettings ? <div className="project-model-scope">
              <strong>{t("models.allowedInProject")}</strong>
              {appSettings.models.providers.flatMap((provider) => provider.enabled ? provider.models
                .filter((model) => model.enabled && (model.status !== "unavailable" || provider.id.startsWith("managed:")) && model.capabilities.text !== "no" && (selected.model_access_policy !== "local_only" || isLocalProvider(provider)))
                .map((model) => {
                  const checked = modelScope.models.some((item) => item.provider_profile_id === provider.id && item.model_id === model.id);
                  return <label key={`${provider.id}:${model.id}`}><input type="checkbox" checked={checked} disabled={action.busy || model.status === "unavailable" || (checked && modelScope.models.length === 1)}
                    title={checked && modelScope.models.length === 1 ? t("buttonAudit.oneModelRequired") : undefined}
                    onChange={(event) => void toggleProjectModel({ provider_profile_id: provider.id, model_id: model.id }, event.target.checked)} />
                    <span>{provider.name} / {model.id}{model.status === "unavailable" ? " · 未加载" : ""}</span>{model.status === "unavailable" && provider.id.startsWith("managed:") && <button type="button" onClick={() => setCurrentPage("models")}>加载模型</button>}</label>;
                }) : [])}
            </div> : null}
            <div className="project-grant">
              <FolderKey size={18} />
              <span title={grant?.directory}>{grant?.directory ?? t("projects.detail.noDirectory")}</span>
              <Button disabled={action.busy} onClick={() => void chooseDirectory()}>{t("projects.detail.authorize")}</Button>
            </div>
            <p>{t("projects.detail.purposeBehavior", { purpose: t(`projects.categories.${selected.category}`) })}</p>
          </div>
          <section className="project-knowledge">
            <h3>{t("models.projectKnowledge")}</h3>
            {knowledge.length === 0 ? <p>{t("models.noKnowledge")}</p> : knowledge.map((item) => (
              <article key={item.id} className="settings-card">
                <strong>{item.pinned ? "📌 " : ""}{item.title}</strong>
                <span>{item.knowledge_type}</span><p>{item.content}</p>
                <div><Button disabled={action.busy} icon={<Pin size={14} />} onClick={() => void updateKnowledge(() => modelRunRepository.pinKnowledge(item.id, !item.pinned))}>{t("models.pin")}</Button>
                  <Button disabled={action.busy} onClick={() => void editKnowledge(item)}>{t("actions.edit")}</Button>
                  <Button disabled={action.busy} icon={<Archive size={14} />} onClick={() => void updateKnowledge(() => modelRunRepository.archiveKnowledge(item.id, true))}>{t("models.archive")}</Button></div>
              </article>
            ))}
          </section>
          <div className="page-heading-row">
            <h3>{t("projects.detail.works")}</h3>
            <Button icon={<Plus size={15} />} onClick={() => startNewWork(selected.id)}>
              {t("projects.detail.newWork")}
            </Button>
          </div>
          {works.length === 0 ? (
            <EmptyState icon={BriefcaseBusiness} title={t("projects.detail.empty")} description={t("projects.detail.emptyDescription")} />
          ) : (
            <div className="project-grid">
              {works.map((work) => (
                <button key={work.id} className="project-card" onClick={() => openConversation(work.id)}
                  {...resourceActions.menuHandlers({ kind: "conversation", id: work.id })}>
                  <BriefcaseBusiness size={21} /><strong>{work.title}</strong>
                  <span>{t("conversation.projectWork")}</span>
                </button>
              ))}
            </div>
          )}
        </section>
      ) : (
        <>
          {!workspaceLoading && !workspaceError && projects.length === 0 ? (
            <EmptyState icon={Folder} title={t("projects.empty.title")} description={t("projects.empty.description")} />
          ) : null}
          <div className="project-grid">
            {projects.map((project) => (
              <button key={project.id} className="project-card" onClick={() => openProject(project.id)}
                {...resourceActions.menuHandlers({ kind: "project", id: project.id })}>
                <Folder size={21} /><strong>{project.name}</strong>
                <span>{t(`projects.categories.${project.category}`)}</span>
              </button>
            ))}
          </div>
        </>
      )}

      {open ? (
        <div className="drawer-backdrop" role="presentation" onMouseDown={() => { if (!saving) setOpen(false); }}>
          <aside className="project-drawer" role="dialog" aria-modal="true" aria-labelledby="new-project-title"
            onMouseDown={(event) => event.stopPropagation()}>
            <div className="drawer-header">
              <h2 id="new-project-title">{t("projects.drawer.title")}</h2>
              <button disabled={saving} aria-label={t("actions.close")} onClick={() => setOpen(false)}><X size={20} /></button>
            </div>
            <form onSubmit={(event) => void submit(event)}>
              <label>{t("projects.drawer.name")}
                <input value={name} disabled={saving || !!createdProjectId} maxLength={80} autoFocus required onChange={(event) => setName(event.target.value)} />
              </label>
              <label>{t("projects.drawer.category")}
                <select value={category} disabled={saving || !!createdProjectId} onChange={(event) => setCategory(event.target.value as ProjectCategory)}>
                  {PROJECT_CATEGORIES.map((item) => <option key={item} value={item}>{t(`projects.categories.${item}`)}</option>)}
                </select>
              </label>
              <label className="checkbox-row"><input type="checkbox" checked={chooseDirectoryOnCreate} disabled={saving}
                onChange={(event) => setChooseDirectoryOnCreate(event.target.checked)} />
                <span>{t("projects.drawer.chooseDirectory")}</span>
              </label>
              {creation.error ? <p className="form-error" role="alert">{creation.error}</p> : null}
              {creation.error && createdProjectId ? <p role="status">{t("buttonAudit.projectCreatedContinue")}</p> : null}
              <div className="drawer-actions">
                <Button disabled={saving} type="button" onClick={() => setOpen(false)}>{t("actions.cancel")}</Button>
                <Button type="submit" variant="primary" disabled={saving || !name.trim()}>{t("actions.create")}</Button>
              </div>
            </form>
          </aside>
        </div>
      ) : null}
    </div>
  );
}
