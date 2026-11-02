import { useEffect, useRef, useState } from "react";
import { DndContext, closestCenter, useDroppable } from "@dnd-kit/core";
import { SortableContext, verticalListSortingStrategy } from "@dnd-kit/sortable";
import {
  BriefcaseBusiness, FolderOpen, FolderPlus, MessageCircle, MoreHorizontal, Pencil, Plus, Trash2,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { useApp } from "../../contexts/AppContext";
import { chatGroupRepository } from "../../services/runtime";
import { useResourceActions } from "../resources/ResourceActionsProvider";
import { useScopedAction } from "../chat/useScopedAction";
import { conversationsIn } from "./treeOperations";
import { SidebarContainerNode } from "./SidebarContainerNode";
import { SidebarConversationNode } from "./SidebarConversationNode";
import { SidebarSectionNode } from "./SidebarSectionNode";
import { useSidebarTreeController, type DragResourceData } from "./useSidebarTreeController";

function UngroupedDropzone({ children }: { children: React.ReactNode }) {
  const data: DragResourceData = {
    entity: "dropzone", containerKind: "ungrouped", containerId: null,
  };
  const drop = useDroppable({ id: "dropzone:ungrouped", data });
  return <div ref={drop.setNodeRef} className={drop.isOver ? "resource-dropzone resource-group--drop" : "resource-dropzone"}>{children}</div>;
}

export function SidebarResourceTree() {
  const { t } = useTranslation("common");
  const app = useApp();
  const controller = useSidebarTreeController();
  const resourceActions = useResourceActions();
  const [scrollState, setScrollState] = useState({ top: false, bottom: false });
  const scrollRef = useRef<HTMLElement>(null);
  const groupAction = useScopedAction("sidebar-groups", t("buttonAudit.actionFailed"));

  const chats = app.conversations.filter((item) => item.kind === "standard_chat");
  const works = app.conversations.filter((item) => item.kind === "project_work");
  const ungrouped = conversationsIn(app.conversations, "standard_chat", null);

  function updateScrollState() {
    const node = scrollRef.current;
    if (!node) return;
    setScrollState({ top: node.scrollTop > 1, bottom: node.scrollTop + node.clientHeight < node.scrollHeight - 1 });
  }
  useEffect(updateScrollState, [app.chatGroups.length, app.projects.length, app.conversations.length]);

  async function createGroup() {
    const name = window.prompt(t("chatGroups.prompt"));
    if (!name?.trim()) return;
    await groupAction.run(async () => { await chatGroupRepository.create(name.trim()); await app.refreshWorkspace(); });
  }
  async function deleteGroup(id: string) {
    if (!window.confirm(t("chatGroups.confirmDelete"))) return;
    await groupAction.run(async () => { await chatGroupRepository.delete(id); await app.refreshWorkspace(); });
  }
  async function renameGroup(id: string, currentName: string) {
    const name = window.prompt(t("chatGroups.renamePrompt"), currentName);
    if (!name?.trim() || name.trim() === currentName) return;
    await groupAction.run(async () => { await chatGroupRepository.rename(id, name.trim()); await app.refreshWorkspace(); });
  }

  return (
    <DndContext
      sensors={controller.sensors}
      collisionDetection={closestCenter}
      onDragOver={controller.onDragOver}
      onDragEnd={controller.onDragEnd}
    >
      <div className={`sidebar-tree-frame${scrollState.top ? " sidebar-tree-frame--top" : ""}${scrollState.bottom ? " sidebar-tree-frame--bottom" : ""}`}>
        <nav ref={scrollRef} className="sidebar-resource-tree" aria-label={t("navigation.resources")} onScroll={updateScrollState}>
          {groupAction.error ? <p className="resource-inline-error" role="alert">{groupAction.error}</p> : null}
          <SidebarSectionNode
            collapsed={controller.preferences.standard_chats_collapsed}
            onToggle={() => controller.toggleSection("standard_chats")}
            title={<><MessageCircle size={15} />{t("navigation.chats")}</>}
            actions={<button disabled={groupAction.busy} title={t("chatGroups.new")} onClick={() => void createGroup()}><FolderPlus size={15} /></button>}
          >
            {(app.workspaceErrors.chatGroups || app.workspaceErrors.conversations) &&
              <button className="resource-inline-error" onClick={() => void app.refreshWorkspace()}>
                {t("sidebar.errors.loadChats")}
              </button>}
            <UngroupedDropzone>
              <SortableContext items={ungrouped.map((item) => item.id)} strategy={verticalListSortingStrategy}>
                {ungrouped.map((chat) => <SidebarConversationNode key={chat.id} conversation={chat}
                  active={app.activeConversationId === chat.id} onOpen={() => app.openConversation(chat.id)} />)}
              </SortableContext>
            </UngroupedDropzone>
            <SortableContext items={app.chatGroups.map((item) => item.id)} strategy={verticalListSortingStrategy}>
              {app.chatGroups.map((group) => {
                const items = conversationsIn(chats, "standard_chat", group.id);
                const collapsed = controller.preferences.collapsed_chat_group_ids.includes(group.id);
                return <SidebarContainerNode key={group.id} id={group.id} kind="chat_group" name={group.name}
                  collapsed={collapsed} onToggle={() => controller.setContainerCollapsed("chat_group", group.id, !collapsed)}
                  actions={<>
                    <button title={t("sidebar.newChatInGroup")} onClick={() => { controller.setContainerCollapsed("chat_group", group.id, false); app.startNewChat(group.id); }}><Plus size={12} /></button>
                    <button disabled={groupAction.busy} title={t("actions.rename")} onClick={() => void renameGroup(group.id, group.name)}><Pencil size={12} /></button>
                    <button disabled={groupAction.busy} title={t("actions.delete")} onClick={() => void deleteGroup(group.id)}><Trash2 size={12} /></button>
                  </>}
                >
                  <SortableContext items={items.map((item) => item.id)} strategy={verticalListSortingStrategy}>
                    {items.map((chat) => <SidebarConversationNode key={chat.id} conversation={chat} depth={2}
                      active={app.activeConversationId === chat.id} onOpen={() => app.openConversation(chat.id)} />)}
                  </SortableContext>
                </SidebarContainerNode>;
              })}
            </SortableContext>
          </SidebarSectionNode>

          <SidebarSectionNode
            collapsed={controller.preferences.projects_collapsed}
            onToggle={() => controller.toggleSection("projects")}
            title={<><BriefcaseBusiness size={15} />{t("navigation.projects")}</>}
            actions={<>
              <button title={t("sidebar.openProjects")} onClick={app.showProjects}><FolderOpen size={14} /></button>
              <button title={t("projects.new")} onClick={app.startNewProject}><Plus size={15} /></button>
            </>}
          >
            {(app.workspaceErrors.projects || app.workspaceErrors.conversations) &&
              <button className="resource-inline-error" onClick={() => void app.refreshWorkspace()}>
                {t("sidebar.errors.loadProjects")}
              </button>}
            <SortableContext items={app.projects.map((item) => item.id)} strategy={verticalListSortingStrategy}>
              {app.projects.map((project) => {
                const items = conversationsIn(works, "project_work", project.id);
                const collapsed = controller.preferences.collapsed_project_ids.includes(project.id);
                return <SidebarContainerNode key={project.id} id={project.id} kind="project" name={project.name}
                  collapsed={collapsed} onOpen={() => app.openProject(project.id)}
                  onToggle={() => controller.setContainerCollapsed("project", project.id, !collapsed)}
                  actions={<>
                    <button title={t("conversation.newWork")} onClick={() => { controller.setContainerCollapsed("project", project.id, false); app.startNewWork(project.id); }}><Plus size={12} /></button>
                    <button title={t("sidebar.moreActions")} aria-label={t("sidebar.moreActions")}
                      onClick={(event) => resourceActions.openMenu({ kind: "project", id: project.id }, event)}><MoreHorizontal size={14} /></button>
                  </>}
                >
                  <SortableContext items={items.map((item) => item.id)} strategy={verticalListSortingStrategy}>
                    {items.map((work) => <SidebarConversationNode key={work.id} conversation={work} depth={2}
                      active={app.activeConversationId === work.id} onOpen={() => app.openConversation(work.id)} />)}
                  </SortableContext>
                </SidebarContainerNode>;
              })}
            </SortableContext>
          </SidebarSectionNode>
        </nav>
      </div>

    </DndContext>
  );
}
