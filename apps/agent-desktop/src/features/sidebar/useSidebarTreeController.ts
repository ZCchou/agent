import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DragEndEvent, DragOverEvent } from "@dnd-kit/core";
import { KeyboardSensor, PointerSensor, useSensor, useSensors } from "@dnd-kit/core";
import { sortableKeyboardCoordinates } from "@dnd-kit/sortable";
import { useTranslation } from "react-i18next";
import { useApp } from "../../contexts/AppContext";
import { chatGroupRepository, conversationRepository, projectRepository, settingsRepository } from "../../services/runtime";
import type { Conversation, ConversationKind } from "../../types/domain";
import type { SidebarPreferences } from "../../types/settings";
import { conversationsIn, orderedConversationIds, placeConversation, reorderEntities } from "./treeOperations";

export type DragResourceData =
  | { entity: "container"; containerKind: "chat_group" | "project"; id: string }
  | { entity: "conversation"; kind: ConversationKind; id: string; containerId: string | null }
  | { entity: "dropzone"; containerKind: "ungrouped" | "chat_group" | "project"; containerId: string | null };

const DEFAULT_PREFERENCES: SidebarPreferences = {
  standard_chats_collapsed: false,
  projects_collapsed: false,
  collapsed_chat_group_ids: [],
  collapsed_project_ids: [],
  width_px: 280,
};

export function useSidebarTreeController() {
  const { t } = useTranslation("common");
  const app = useApp();
  const [preferences, setPreferences] = useState(DEFAULT_PREFERENCES);
  const preferenceRevision = useRef(0);
  const savedPreferences = useRef(DEFAULT_PREFERENCES);
  const preferenceWrites = useRef(0);
  const hoverTimer = useRef<number | null>(null);
  const hoveredContainer = useRef<string | null>(null);
  const lastOpenedResource = useRef<string | null>(null);
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  useEffect(() => {
    if (app.workspaceLoading) return;
    let active = true;
    const revision = preferenceRevision.current;
    void settingsRepository.getAppSettings().then((settings) => {
      if (!active || revision !== preferenceRevision.current || preferenceWrites.current > 0) return;
      const groupIds = new Set(app.chatGroups.map((item) => item.id));
      const projectIds = new Set(app.projects.map((item) => item.id));
      const loaded = {
        ...settings.sidebar,
        collapsed_chat_group_ids: settings.sidebar.collapsed_chat_group_ids.filter((id) => groupIds.has(id)),
        collapsed_project_ids: settings.sidebar.collapsed_project_ids.filter((id) => projectIds.has(id)),
      };
      savedPreferences.current = loaded;
      setPreferences(loaded);
    }).catch(() => undefined);
    return () => { active = false; };
  }, [app.workspaceLoading]);

  const savePreferences = useCallback((next: SidebarPreferences) => {
    const revision = ++preferenceRevision.current;
    preferenceWrites.current += 1;
    setPreferences(next);
    void settingsRepository.updateSidebarPreferences(next).then((settings) => {
      savedPreferences.current = settings.sidebar;
    }).catch(() => {
      if (revision === preferenceRevision.current) {
        setPreferences(savedPreferences.current);
        window.alert(t("sidebar.errors.saveCollapse"));
      }
    }).finally(() => { preferenceWrites.current -= 1; });
  }, [t]);

  const toggleSection = useCallback((section: "standard_chats" | "projects") => {
    savePreferences({ ...preferences,
      [section === "standard_chats" ? "standard_chats_collapsed" : "projects_collapsed"]:
        !preferences[section === "standard_chats" ? "standard_chats_collapsed" : "projects_collapsed"],
    });
  }, [preferences, savePreferences]);

  const setContainerCollapsed = useCallback((kind: "chat_group" | "project", id: string, collapsed: boolean) => {
    const key = kind === "chat_group" ? "collapsed_chat_group_ids" : "collapsed_project_ids";
    const values = new Set(preferences[key]);
    if (collapsed) values.add(id); else values.delete(id);
    savePreferences({ ...preferences, [key]: [...values] });
  }, [preferences, savePreferences]);

  useEffect(() => {
    const resourceKey = app.activeConversationId ?? (app.viewingProjectId ? `project:${app.viewingProjectId}` : null);
    if (!resourceKey || resourceKey === lastOpenedResource.current) return;
    lastOpenedResource.current = resourceKey;
    const active = app.conversations.find((item) => item.id === app.activeConversationId);
    const projectId = active?.project_id ?? app.viewingProjectId;
    const groupId = active?.chat_group_id;
    let next = preferences;
    if (active?.kind === "standard_chat" && preferences.standard_chats_collapsed) {
      next = { ...next, standard_chats_collapsed: false };
    }
    if ((active?.kind === "project_work" || projectId) && preferences.projects_collapsed) {
      next = { ...next, projects_collapsed: false };
    }
    if (groupId && next.collapsed_chat_group_ids.includes(groupId)) {
      next = { ...next, collapsed_chat_group_ids: next.collapsed_chat_group_ids.filter((id) => id !== groupId) };
    }
    if (projectId && next.collapsed_project_ids.includes(projectId)) {
      next = { ...next, collapsed_project_ids: next.collapsed_project_ids.filter((id) => id !== projectId) };
    }
    if (next !== preferences) savePreferences(next);
  }, [app.activeConversationId, app.viewingProjectId, app.conversations, preferences, savePreferences]);

  const destinationFor = useCallback((over: DragResourceData) => {
    if (over.entity === "conversation") return { kind: over.kind, containerId: over.containerId };
    if (over.entity === "dropzone") return {
      kind: over.containerKind === "project" ? "project_work" as const : "standard_chat" as const,
      containerId: over.containerId,
    };
    return {
      kind: over.containerKind === "project" ? "project_work" as const : "standard_chat" as const,
      containerId: over.id,
    };
  }, []);

  const persistConversationMove = useCallback(async (
    moving: Conversation, destinationKind: ConversationKind, destinationId: string | null,
    targetIndex: number,
  ) => {
    if (moving.kind !== destinationKind) return;
    const oldContainer = moving.kind === "standard_chat" ? moving.chat_group_id : moving.project_id;
    if (moving.kind === "project_work" && oldContainer !== destinationId) {
      const source = app.projects.find((item) => item.id === oldContainer)?.name ?? "";
      const target = app.projects.find((item) => item.id === destinationId)?.name ?? "";
      if (!destinationId || !window.confirm(t("sidebar.confirmMoveWork", { source, target }))) return;
    }
    const snapshot = { projects: app.projects, chatGroups: app.chatGroups, conversations: app.conversations };
    const optimistic = placeConversation(app.conversations, moving.id, {
      kind: moving.kind, containerId: destinationId, targetIndex,
    });
    app.replaceWorkspaceResources({ ...snapshot, conversations: optimistic });
    try {
      if (moving.kind === "standard_chat") {
        await conversationRepository.moveChat(moving.id, destinationId, targetIndex);
      } else if (oldContainer !== destinationId) {
        await conversationRepository.moveWork(moving.id, destinationId!, targetIndex);
      } else {
        await conversationRepository.reorder({
          kind: moving.kind, containerId: destinationId,
          orderedIds: orderedConversationIds(optimistic, moving.kind, destinationId),
        });
      }
      if (destinationId) setContainerCollapsed(
        moving.kind === "standard_chat" ? "chat_group" : "project", destinationId, false,
      );
    } catch {
      app.replaceWorkspaceResources(snapshot);
      window.alert(t("sidebar.errors.moveFailed"));
    }
  }, [app, setContainerCollapsed, t]);

  const onDragEnd = useCallback((event: DragEndEvent) => {
    if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current);
    hoveredContainer.current = null;
    if (!event.over) return;
    const active = event.active.data.current as DragResourceData;
    const over = event.over.data.current as DragResourceData;
    if (!active || !over || event.active.id === event.over.id) return;
    if (active.entity === "container") {
      if (over.entity !== "container" || active.containerKind !== over.containerKind) return;
      const snapshot = { projects: app.projects, chatGroups: app.chatGroups, conversations: app.conversations };
      const isGroup = active.containerKind === "chat_group";
      const nextGroups = isGroup ? reorderEntities(app.chatGroups, active.id, over.id) : app.chatGroups;
      const nextProjects = isGroup ? app.projects : reorderEntities(app.projects, active.id, over.id);
      app.replaceWorkspaceResources({ ...snapshot, projects: nextProjects, chatGroups: nextGroups });
      const request = isGroup
        ? chatGroupRepository.reorder(nextGroups.map((item) => item.id))
        : projectRepository.reorder(nextProjects.map((item) => item.id));
      void request.catch(() => {
        app.replaceWorkspaceResources(snapshot);
        window.alert(t("sidebar.errors.reorderFailed"));
      });
      return;
    }
    if (active.entity !== "conversation") return;
    const moving = app.conversations.find((item) => item.id === active.id);
    if (!moving) return;
    const destination = destinationFor(over);
    if (destination.kind !== moving.kind) return;
    const items = conversationsIn(app.conversations, moving.kind, destination.containerId);
    const overIndex = over.entity === "conversation" ? items.findIndex((item) => item.id === over.id) : 0;
    const targetIndex = overIndex < 0 ? 0 : overIndex;
    void persistConversationMove(moving, destination.kind, destination.containerId, targetIndex);
  }, [app, destinationFor, persistConversationMove, t]);

  const onDragOver = useCallback((event: DragOverEvent) => {
    if (!event.over) {
      if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current);
      hoveredContainer.current = null;
      return;
    }
    const over = event.over.data.current as DragResourceData;
    if (!over || over.entity !== "container") return;
    const key = `${over.containerKind}:${over.id}`;
    if (hoveredContainer.current === key) return;
    if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current);
    hoveredContainer.current = key;
    hoverTimer.current = window.setTimeout(() => {
      setContainerCollapsed(over.containerKind, over.id, false);
      hoverTimer.current = null;
    }, 600);
  }, [setContainerCollapsed]);

  const moveWork = useCallback(async (work: Conversation, projectId: string) => {
    await persistConversationMove(work, "project_work", projectId, 0);
  }, [persistConversationMove]);

  const moveChat = useCallback(async (chat: Conversation, chatGroupId: string | null) => {
    await persistConversationMove(chat, "standard_chat", chatGroupId, 0);
  }, [persistConversationMove]);

  const copyWork = useCallback(async (work: Conversation, projectId: string) => {
    try {
      const copy = await conversationRepository.copyWork(work.id, projectId);
      await app.refreshWorkspace();
      app.openConversation(copy.id);
    } catch { window.alert(t("sidebar.errors.copyFailed")); }
  }, [app, t]);

  return useMemo(() => ({
    sensors, preferences, toggleSection, setContainerCollapsed,
    onDragEnd, onDragOver, moveChat, moveWork, copyWork,
  }), [sensors, preferences, toggleSection, setContainerCollapsed, onDragEnd, onDragOver, moveChat, moveWork, copyWork]);
}
