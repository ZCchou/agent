import { createContext, useCallback, useContext, useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import { Copy, FolderOpen, MoveRight, Pencil, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useApp } from "../../contexts/AppContext";
import { conversationRepository, projectDirectoryRepository, projectRepository, settingsRepository, workToolRepository } from "../../services/runtime";
import { ResourceContextMenu, ResourceDialog } from "./ResourceOverlays";

type Target = { kind: "project" | "conversation"; id: string };
type TriggerEvent = MouseEvent<HTMLElement> | KeyboardEvent<HTMLElement>;
type Action = "menu" | "rename" | "move" | "copy" | "delete";
interface Actions {
  openMenu(target: Target, event: TriggerEvent): void;
  rename(target: Target): void;
  menuHandlers(target: Target): {
    onContextMenu(event: MouseEvent<HTMLElement>): void;
    onKeyDown(event: KeyboardEvent<HTMLElement>): void;
  };
}
const Context = createContext<Actions | null>(null);

export function ResourceActionsProvider({ children }: { children: ReactNode }) {
  const app = useApp();
  const { t } = useTranslation("common");
  const [state, setState] = useState<{ target: Target; action: Action; trigger: HTMLElement | null; position: { x: number; y: number } } | null>(null);
  const [name, setName] = useState("");
  const [selectedContainer, setSelectedContainer] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const project = state?.target.kind === "project" ? app.projects.find((p) => p.id === state.target.id) : null;
  const conversation = state?.target.kind === "conversation" ? app.conversations.find((c) => c.id === state.target.id) : null;
  const title = project?.name ?? conversation?.title ?? "";
  const close = useCallback(() => { if (!busyRef.current) setState(null); }, []);

  function show(target: Target, action: Action, trigger: HTMLElement | null, position = { x: 0, y: 0 }) {
    if (busyRef.current) return;
    const item = target.kind === "project" ? app.projects.find((p) => p.id === target.id)
      : app.conversations.find((c) => c.id === target.id);
    if (!item) return;
    setName("name" in item ? item.name : item.title);
    setError(null);
    setSelectedContainer("kind" in item ? (item.kind === "standard_chat" ? item.chat_group_id : item.project_id) ?? "" : "");
    setState({ target, action, trigger, position });
  }
  function openMenu(target: Target, event: TriggerEvent) {
    event.preventDefault(); event.stopPropagation();
    const element = event.target instanceof Element ? event.target.closest<HTMLElement>("button") : null;
    const trigger = element && event.currentTarget.contains(element) ? element : event.currentTarget;
    const bounds = trigger.getBoundingClientRect();
    const x = "clientX" in event ? event.clientX : NaN;
    const y = "clientY" in event ? event.clientY : NaN;
    const pointer = Number.isFinite(x) && Number.isFinite(y) && (x !== 0 || y !== 0);
    show(target, "menu", trigger, pointer ? { x, y } : { x: bounds.left, y: bounds.bottom });
  }
  function rename(target: Target) {
    show(target, "rename", document.activeElement instanceof HTMLElement ? document.activeElement : null);
  }
  function menuHandlers(target: Target) {
    return {
      onContextMenu: (event: MouseEvent<HTMLElement>) => openMenu(target, event),
      onKeyDown: (event: KeyboardEvent<HTMLElement>) => {
        if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) openMenu(target, event);
      },
    };
  }
  function changeAction(action: Action) { setError(null); setState((s) => s ? { ...s, action } : s); }

  async function perform(operation: () => Promise<void>, errorKey: string) {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(null);
    try { await operation(); }
    catch { setError(t(errorKey)); }
    finally { busyRef.current = false; setBusy(false); }
  }
  async function saveName() {
    const trimmed = name.trim();
    if (!trimmed || trimmed.length > 80 || trimmed === title || !state) return;
    await perform(async () => {
      if (project) await projectRepository.update(project.id, trimmed, project.category);
      else if (conversation) await conversationRepository.update(conversation.id, {
        title: trimmed, category: conversation.kind === "standard_chat" ? conversation.category ?? conversation.resolved_category : undefined,
      });
      else return;
      await app.refreshWorkspace();
      setState(null);
    }, "sidebar.errors.renameFailed");
  }
  async function openProjectDirectory() {
    if (!project) return;
    await perform(async () => {
      const grant = await workToolRepository.getProjectGrant(project.id);
      if (!grant || !grant.active) {
        setError(t("sidebar.errors.directoryNotAuthorized"));
        return;
      }
      await projectDirectoryRepository.openRoot(project.id);
      setState(null);
    }, "sidebar.errors.openDirectoryFailed");
  }
  async function deleteProject() {
    if (!project) return;
    await perform(async () => {
      const impact = await projectRepository.deleteImpact(project.id);
      if (!window.confirm(t("sidebar.confirmDeleteProject", {
        name: project.name, workCount: impact.work_count, messageCount: impact.message_count,
      })) || !window.confirm(t("sidebar.confirmDeleteProjectAgain", { name: project.name }))) return;
      await projectRepository.delete(project.id);
      await app.refreshWorkspace();
      if (app.activeConversation?.project_id === project.id) app.startNewChat();
      if (app.viewingProjectId === project.id) app.showProjects();
      setState(null);
    }, "sidebar.errors.deleteFailed");
  }
  async function deleteConversation() {
    if (!conversation) return;
    await perform(async () => {
      if (!window.confirm(t("sidebar.confirmDeleteConversation", { title: conversation.title }))) return;
      await conversationRepository.delete(conversation.id);
      await app.refreshWorkspace();
      if (app.activeConversationId === conversation.id) app.startNewChat();
      setState(null);
    }, "sidebar.errors.deleteFailed");
  }
  async function moveOrCopy() {
    if (!conversation || !state) return;
    const action = state.action;
    await perform(async () => {
      if (action === "copy") {
        if (!selectedContainer) return;
        const copy = await conversationRepository.copyWork(conversation.id, selectedContainer);
        await expandDestination("project", selectedContainer);
        await app.refreshWorkspace();
        app.openConversation(copy.id);
      } else {
        if (conversation.kind === "standard_chat") {
          await conversationRepository.moveChat(conversation.id, selectedContainer || null, 0);
        } else {
          if (!selectedContainer) return;
          const source = app.projects.find((p) => p.id === conversation.project_id)?.name ?? "";
          const target = app.projects.find((p) => p.id === selectedContainer)?.name ?? "";
          if (!window.confirm(t("sidebar.confirmMoveWork", { source, target }))) return;
          await conversationRepository.moveWork(conversation.id, selectedContainer, 0);
        }
        await expandDestination(conversation.kind === "standard_chat" ? "chat_group" : "project", selectedContainer);
        await app.refreshWorkspace();
      }
      setState(null);
    }, action === "copy" ? "sidebar.errors.copyFailed" : "sidebar.errors.moveFailed");
  }
  async function expandDestination(kind: "chat_group" | "project", id: string) {
    if (!id) return;
    try {
      const { sidebar } = await settingsRepository.getAppSettings();
      const key = kind === "chat_group" ? "collapsed_chat_group_ids" : "collapsed_project_ids";
      if (sidebar[key].includes(id)) await settingsRepository.updateSidebarPreferences({
        ...sidebar, [key]: sidebar[key].filter((value) => value !== id),
      });
    } catch {
      // The resource has already moved; preference failure must not report the move as failed.
      window.alert(t("sidebar.errors.saveCollapse"));
    }
  }
  const currentContainer = conversation?.kind === "standard_chat" ? conversation.chat_group_id ?? "" : conversation?.project_id ?? "";
  const validMove = state?.action === "copy" ? !!selectedContainer : selectedContainer !== currentContainer;
  const renameValid = !!name.trim() && name.trim().length <= 80 && name.trim() !== title;
  return <Context.Provider value={{ openMenu, rename, menuHandlers }}>
    {children}
    {state && (project || conversation) && (state.action === "menu" ? <ResourceContextMenu
      label={title} trigger={state.trigger} position={state.position} onClose={close} busy={busy}>
      <button role="menuitem" disabled={busy} onClick={() => changeAction("rename")}><Pencil size={15} />{t("actions.rename")}</button>
      {project && <button role="menuitem" disabled={busy} onClick={() => void openProjectDirectory()}><FolderOpen size={15} />{t("sidebar.openDirectory")}</button>}
      {conversation && <button role="menuitem" disabled={busy} onClick={() => changeAction("move")}><MoveRight size={15} />{conversation.kind === "standard_chat" ? t("sidebar.moveToGroup") : t("sidebar.moveToProject")}</button>}
      {conversation?.kind === "project_work" && <button role="menuitem" disabled={busy} onClick={() => changeAction("copy")}><Copy size={15} />{t("sidebar.copyToProject")}</button>}
      <button role="menuitem" className="resource-action-menu__danger" disabled={busy}
        onClick={() => project ? void deleteProject() : changeAction("delete")}><Trash2 size={15} />{t("actions.delete")}</button>
      {error && <p className="resource-action-error" role="alert">{error}</p>}
    </ResourceContextMenu> : <ResourceDialog label={state.action === "rename" ? t("actions.rename") : t("sidebar.manageResource")}
      trigger={state.trigger} busy={busy} onClose={close}>
      <h3 title={title}>{state.action === "rename" ? t("actions.rename") : title}</h3>
      {state.action === "rename" ? <form className="resource-rename-form" onSubmit={(event) => { event.preventDefault(); void saveName(); }}>
        <label>{project ? t("projects.drawer.name") : t("sidebar.conversationTitle")}
          <input value={name} maxLength={80} disabled={busy} onChange={(event) => { setName(event.target.value); setError(null); }} />
        </label>
        {error && <p className="resource-action-error" role="alert">{error}</p>}
        <div className="resource-dialog__actions">
          <button type="button" disabled={busy} onClick={close}>{t("actions.cancel")}</button>
          <button type="submit" className="resource-action-primary" disabled={busy || !renameValid}>{busy ? t("actions.saving") : t("actions.save")}</button>
        </div>
      </form> : state.action === "delete" ? <>
        <p>{t("sidebar.deleteConversationWarning")}</p>
        {error && <p className="resource-action-error" role="alert">{error}</p>}
        <div className="resource-dialog__actions">
          <button disabled={busy} onClick={() => changeAction("menu")}>{t("actions.back")}</button>
          <button className="danger-button" disabled={busy} onClick={() => void deleteConversation()}>{t("actions.delete")}</button>
        </div>
      </> : <>
        <label>{conversation?.kind === "standard_chat" ? t("sidebar.chooseChatGroup") : t("sidebar.chooseProject")}
          <select value={selectedContainer} disabled={busy} onChange={(event) => setSelectedContainer(event.target.value)}>
            {conversation?.kind === "standard_chat" && <option value="">{t("sidebar.ungrouped")}</option>}
            {(conversation?.kind === "standard_chat" ? app.chatGroups : app.projects).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
        {conversation?.kind === "project_work" && <p>{state.action === "move" ? t("sidebar.moveWorkWarning") : t("sidebar.copyWorkWarning")}</p>}
        {error && <p className="resource-action-error" role="alert">{error}</p>}
        <div className="resource-dialog__actions">
          <button disabled={busy} onClick={() => changeAction("menu")}>{t("actions.back")}</button>
          <button disabled={busy || !validMove} onClick={() => void moveOrCopy()}>{state.action === "copy" ? t("sidebar.copyToProject") : t("sidebar.move")}</button>
        </div>
      </>}
    </ResourceDialog>)}
  </Context.Provider>;
}

export function useResourceActions() {
  const context = useContext(Context);
  if (!context) throw new Error("ResourceActionsProvider is required");
  return context;
}
