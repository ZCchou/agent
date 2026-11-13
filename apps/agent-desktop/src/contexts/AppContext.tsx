import {
  createContext, useCallback, useContext, useEffect, useMemo, useReducer, useState, type ReactNode,
} from "react";
import type { AppPage } from "../app/navigation";
import { INITIAL_PROJECT_NAVIGATION, projectNavigationReducer } from "../app/projectNavigation";
import type {
  ChatGroup, Conversation, ConversationKind, Project, ProjectCategory,
} from "../types/domain";
import {
  chatGroupRepository, conversationRepository, projectRepository,
} from "../services/runtime";
import { getAgentCoreStatus, pingAgentCore, type CoreStatus } from "../services/tauri/coreBridge";

interface DraftConversation {
  ragMode?: "auto" | "required";
  kind: ConversationKind;
  category: ProjectCategory;
  projectId: string | null;
  chatGroupId: string | null;
  revision: number;
}

export interface WorkspaceResources {
  projects: Project[];
  chatGroups: ChatGroup[];
  conversations: Conversation[];
}

interface AppContextValue {
  currentPage: AppPage;
  setCurrentPage: (page: AppPage) => void;
  projects: Project[];
  chatGroups: ChatGroup[];
  conversations: Conversation[];
  workspaceLoading: boolean;
  workspaceError: boolean;
  workspaceErrors: WorkspaceErrors;
  refreshWorkspace: () => Promise<void>;
  activeConversationId: string | null;
  activeConversation: Conversation | null;
  openConversation: (id: string) => void;
  viewingProjectId: string | null;
  openProject: (id: string) => void;
  showProjects: () => void;
  startNewProject: () => void;
  newProjectRequested: boolean;
  consumeNewProjectRequest: () => void;
  draft: DraftConversation;
  setDraftCategory: (category: ProjectCategory) => void;
  setDraftRagMode: (mode: "auto" | "required") => void;
  startNewChat: (chatGroupId?: string | null) => void;
  startNewWork: (projectId: string) => void;
  conversationCreated: (conversation: Conversation) => Promise<void>;
  coreStatus: CoreStatus;
  coreOnline: boolean;
  refreshCoreStatus: () => Promise<void>;
  replaceWorkspaceResources: (resources: WorkspaceResources) => void;
}

export interface WorkspaceErrors {
  projects: boolean;
  chatGroups: boolean;
  conversations: boolean;
}

export const AppContext = createContext<AppContextValue | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const [currentPage, setCurrentPage] = useState<AppPage>("chat");
  const [projects, setProjects] = useState<Project[]>([]);
  const [chatGroups, setChatGroups] = useState<ChatGroup[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [workspaceLoading, setWorkspaceLoading] = useState(true);
  const [workspaceError, setWorkspaceError] = useState(false);
  const [workspaceErrors, setWorkspaceErrors] = useState<WorkspaceErrors>({
    projects: false, chatGroups: false, conversations: false,
  });
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [projectNavigation, dispatchProjectNavigation] = useReducer(
    projectNavigationReducer,
    INITIAL_PROJECT_NAVIGATION,
  );
  const { viewingProjectId, newProjectRequested } = projectNavigation;
  const [draft, setDraft] = useState<DraftConversation>({
    kind: "standard_chat", category: "casual", projectId: null, chatGroupId: null, revision: 0,
  });
  const [coreStatus, setCoreStatus] = useState<CoreStatus>({ running: false, healthy: false });

  const refreshCoreStatus = useCallback(async () => {
    try {
      let status = await getAgentCoreStatus();
      if (status.running) {
        let issue: string | null = null;
        try { await pingAgentCore(); } catch (error) { issue = String(error); }
        status = { ...(await getAgentCoreStatus()), issue };
      }
      setCoreStatus(status);
    } catch {
      setCoreStatus({ running: false, healthy: false, issue: null });
    }
  }, []);

  const refreshWorkspace = useCallback(async () => {
    setWorkspaceLoading(true);
    try {
      const [projectResult, groupResult, conversationResult] = await Promise.allSettled([
        projectRepository.list(),
        chatGroupRepository.list(),
        conversationRepository.list(),
      ]);
      if (projectResult.status === "fulfilled") setProjects(projectResult.value);
      if (groupResult.status === "fulfilled") setChatGroups(groupResult.value);
      if (conversationResult.status === "fulfilled") setConversations(conversationResult.value);
      const errors = {
        projects: projectResult.status === "rejected",
        chatGroups: groupResult.status === "rejected",
        conversations: conversationResult.status === "rejected",
      };
      setWorkspaceErrors(errors);
      setWorkspaceError(Object.values(errors).some(Boolean));
    } finally {
      setWorkspaceLoading(false);
    }
  }, []);

  const startNewChat = useCallback((chatGroupId: string | null = null) => {
    dispatchProjectNavigation({ type: "view", projectId: null });
    setActiveConversationId(null);
    setDraft((current) => ({
      kind: "standard_chat", category: "casual", projectId: null, chatGroupId,
      revision: current.revision + 1,
    }));
    setCurrentPage("chat");
  }, []);

  const startNewWork = useCallback((projectId: string) => {
    dispatchProjectNavigation({ type: "view", projectId });
    setActiveConversationId(null);
    setDraft((current) => ({
      kind: "project_work", category: "casual", projectId, chatGroupId: null,
      revision: current.revision + 1,
    }));
    setCurrentPage("chat");
  }, []);

  const openConversation = useCallback((id: string) => {
    setActiveConversationId(id);
    const selected = conversations.find((conversation) => conversation.id === id);
    dispatchProjectNavigation({ type: "view", projectId: selected?.project_id ?? null });
    setCurrentPage("chat");
  }, [conversations]);

  const openProject = useCallback((id: string) => {
    dispatchProjectNavigation({ type: "view", projectId: id });
    setCurrentPage("projects");
  }, []);

  const showProjects = useCallback(() => {
    dispatchProjectNavigation({ type: "view", projectId: null });
    setCurrentPage("projects");
  }, []);

  const startNewProject = useCallback(() => {
    dispatchProjectNavigation({ type: "request_new" });
    setCurrentPage("projects");
  }, []);

  const consumeNewProjectRequest = useCallback(() => {
    dispatchProjectNavigation({ type: "consume_new" });
  }, []);

  const conversationCreated = useCallback(async (conversation: Conversation) => {
    setActiveConversationId(conversation.id);
    dispatchProjectNavigation({ type: "view", projectId: conversation.project_id });
    await refreshWorkspace();
  }, [refreshWorkspace]);

  const replaceWorkspaceResources = useCallback((resources: WorkspaceResources) => {
    setProjects(resources.projects);
    setChatGroups(resources.chatGroups);
    setConversations(resources.conversations);
  }, []);

  useEffect(() => {
    void refreshCoreStatus();
    const timer = window.setInterval(() => void refreshCoreStatus(), 5_000);
    return () => window.clearInterval(timer);
  }, [refreshCoreStatus]);

  useEffect(() => {
    if (coreStatus.healthy) void refreshWorkspace();
    else setWorkspaceLoading(false);
  }, [coreStatus.healthy, refreshWorkspace]);

  const activeConversation = conversations.find(
    (conversation) => conversation.id === activeConversationId,
  ) ?? null;

  const value = useMemo<AppContextValue>(() => ({
    currentPage, setCurrentPage,
    projects, chatGroups, conversations, workspaceLoading, workspaceError, workspaceErrors, refreshWorkspace,
    activeConversationId, activeConversation, openConversation,
    viewingProjectId, openProject, showProjects, startNewProject,
    newProjectRequested, consumeNewProjectRequest,
    draft,
    setDraftCategory: (category) => setDraft((current) => ({ ...current, category })),
    setDraftRagMode: (ragMode) => setDraft((current) => ({ ...current, ragMode })),
    startNewChat, startNewWork, conversationCreated,
    coreStatus, coreOnline: coreStatus.healthy, refreshCoreStatus,
    replaceWorkspaceResources,
  }), [
    currentPage, projects, chatGroups, conversations, workspaceLoading, workspaceError, workspaceErrors,
    refreshWorkspace, activeConversationId, activeConversation, openConversation,
    viewingProjectId, openProject, showProjects, startNewProject,
    newProjectRequested, consumeNewProjectRequest,
    draft, startNewChat, startNewWork, conversationCreated,
    coreStatus, refreshCoreStatus, replaceWorkspaceResources,
  ]);

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp() {
  const context = useContext(AppContext);
  if (!context) throw new Error("useApp must be used inside AppProvider");
  return context;
}
