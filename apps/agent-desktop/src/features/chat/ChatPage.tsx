import { useUpdateDraft } from "../updater/useUpdateDraft";
import { useAppUpdate } from "../updater/AppUpdateProvider";
import { ArrowDown, Bot, GitBranch, Pencil, RotateCcw, Upload, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useConversationState } from "./useConversationState";
import { useScopedAction } from "./useScopedAction";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";
import { useApp } from "../../contexts/AppContext";
import { useResourceActions } from "../resources/ResourceActionsProvider";
import { useChatInspectorStore } from "../../contexts/ChatInspectorContext";
import {
  agentExecutionRepository, attachmentRepository, changeSetRepository, conversationRepository, modelCatalogRepository,
  contextRepository, modelRunRepository, projectDirectoryRepository, projectFilePreviewRepository, projectRepository, providerRepository, settingsRepository, workToolRepository,
  webSearchRepository,
} from "../../services/runtime";
import {
  PROJECT_CATEGORIES,
  type ChatMessage,
  type Conversation,
  type ProjectCategory,
  type ProjectGrant,
  type ChangeSet,
  type ContextCompactionState,
  type CompactionTargetRequest,
  type ModelRun,
  type ModelRunEvent,
  type ToolOperation,
  type MessageAttachment,
  type WebOperation,
  type ExecutionPlan,
  type CapabilityAdvisory,
} from "../../types/domain";
import { resolveSelectedModel, selectableModels } from "../../services/modelSelection";
import { ComposerDock } from "./ComposerDock";
import { ChatInspector } from "./ChatInspector";
import { ConversationTimeline } from "./ConversationTimeline";
import { WorkProcessDisclosure } from "./WorkProcessDisclosure";
import { AssistantRunMessage } from "./AssistantRunMessage";
import { useStreamingMarkdownBuffer } from "./useStreamingMarkdownBuffer";
import { useAttachmentController } from "./useAttachmentController";
import { useAttachmentReferences } from "./useAttachmentReferences";
import { AttachmentReferenceTray } from "./AttachmentReferenceTray";
import { useConversationScroll } from "./useConversationScroll";
import type { ProjectDirectoryTarget, ProjectFileTarget } from "./markdown/markdownPolicy";
import { useWebSearchController } from "../web-search/useWebSearchController";
import { VisionCapabilityPrompt } from "./VisionCapabilityPrompt";
import { RoutingBudgetPrompt } from "./RoutingBudgetPrompt";
import { ConversationRoutingControl, type RoutingModelOption } from "./ConversationRoutingControl";
import { applyRunFailure, runErrorTranslationKey } from "./runErrors";

const MODEL_RUN_STATUSES = new Set<ModelRun["status"]>([
  "queued", "building_context", "waiting_context", "waiting_permission", "waiting_budget", "generating", "waiting_tool",
  "completed", "failed", "cancelled", "interrupted",
]);

function isModelRunStatus(value: unknown): value is ModelRun["status"] {
  return typeof value === "string" && MODEL_RUN_STATUSES.has(value as ModelRun["status"]);
}

export function ChatPage() {
  const { installing } = useAppUpdate();
  const resourceActions = useResourceActions();
  const { t: tChat } = useTranslation("chat");
  const { t: tCommon } = useTranslation("common");
  const {
    currentPage, setCurrentPage, activeConversationId, activeConversation, projects, draft, coreOnline,
    openConversation, openProject, conversationCreated, refreshWorkspace,
  } = useApp();
  const inspectorStore = useChatInspectorStore();
  const updateInspector = inspectorStore.update;
  const inspectorKey = activeConversationId ?? `draft-${draft.revision}`;
  const inspector = inspectorStore.get(inspectorKey);
  const inspectorRequest = useRef(0);
  const [conversation, setConversation] = useConversationState<Conversation | null>(inspectorKey, null);
  const workspaceConversationRef = useRef(activeConversation);
  workspaceConversationRef.current = activeConversation;
  const previousPageRef = useRef(currentPage);
  const conversationTitle = activeConversation?.id === conversation?.id ? activeConversation?.title : conversation?.title;
  const [messages, setMessages] = useConversationState<ChatMessage[]>(inspectorKey, []);
  const [input, setInput] = useConversationState(inspectorKey, "");
  const references = useAttachmentReferences(inspectorKey, input, messages);
  const [loading, setLoading] = useConversationState(inspectorKey, false);
  const [sending, setSending] = useConversationState(inspectorKey, false);
  const [stopping, setStopping] = useConversationState(inspectorKey, false);
  const stoppingRef = useRef(false);
  const runActionRef = useRef(0);
  const sendScopeRef = useRef(inspectorKey);
  const adoptedScopeRef = useRef<string | null>(null);
  sendScopeRef.current = inspectorKey;
  const submissionRef = useRef<{
    optimisticId: string; scope: string; acceptedScope?: string; accepted: boolean; stopRequested: boolean;
  } | null>(null);
  const attachmentController = useAttachmentController(inspectorKey, currentPage === "chat" && coreOnline && !sending && !installing);
  const [error, setError] = useConversationState(inspectorKey, false);
  const [errorDetail, setErrorDetail] = useConversationState<string | null>(inspectorKey, null);
  const [failedContent, setFailedContent] = useConversationState<string | null>(inspectorKey, null);
  const [failedEditMessage, setFailedEditMessage] = useConversationState<ChatMessage | null>(inspectorKey, null);
  const [changeSets, setChangeSets] = useConversationState<ChangeSet[]>(inspectorKey, []);
  const [toolOperations, setToolOperations] = useConversationState<ToolOperation[]>(inspectorKey, []);
  const toolAuditRequest = useRef(0);
  const toolAuditConversation = useRef<string | null>(null);
  const [webOperations, setWebOperations] = useConversationState<WebOperation[]>(inspectorKey, []);
  const [activeRun, setActiveRun] = useConversationState<ModelRun | null>(inspectorKey, null);
  const [compaction, setCompaction] = useConversationState<ContextCompactionState | null>(inspectorKey, null);
  const compactionBlocksSend = Boolean(compaction && ["queued", "running", "interrupted", "failed"].includes(compaction.status));
  const compactionResumeRef = useRef(false);
  const {
    content: streamingContent,
    append: appendStreamingContent,
    replace: replaceStreamingContent,
    clear: clearStreamingContent,
  } = useStreamingMarkdownBuffer();
  const [lastRunId, setLastRunId] = useConversationState<string | null>(inspectorKey, null);
  const [knowledgeProposal, setKnowledgeProposal] = useConversationState<{ id: string; title: string; content: string } | null>(inspectorKey, null);
  const runIdRef = useRef<string | null>(null);
  const terminalRefreshRevision = useRef(0);
  const [grant, setGrant] = useConversationState<ProjectGrant | null>(inspectorKey, null);
  const [deriveOpen, setDeriveOpen] = useConversationState(inspectorKey, false);
  const [deriveProject, setDeriveProject] = useConversationState(inspectorKey, "");
  const [newProjectName, setNewProjectName] = useConversationState(inspectorKey, "");
  const [newProjectCategory, setNewProjectCategory] = useConversationState<ProjectCategory>(inspectorKey, "work");
  const knowledgeAction = useScopedAction(inspectorKey, tCommon("buttonAudit.actionFailed"));
  const deriveAction = useScopedAction(inspectorKey, tCommon("buttonAudit.actionFailed"));
  const derivedProjectRef = useRef<{ scope: string; id: string } | null>(null);
  const derivedWorkRef = useRef<{ scope: string; id: string } | null>(null);
  const [forceBottomVersion, setForceBottomVersion] = useConversationState(inspectorKey, 0);
  const [pendingSearchSetupRevision, setPendingSearchSetupRevision] = useConversationState<{ connection: number; permission: number } | null>(inspectorKey, null);
  const [pendingVisionConfirmation, setPendingVisionConfirmation] = useConversationState<{
    content: string; providerId: string; modelId: string;
  } | null>(inspectorKey, null);
  const [visionConfirmationBusy, setVisionConfirmationBusy] = useConversationState(inspectorKey, false);
  const [pendingBudgetPlan, setPendingBudgetPlan] = useConversationState<ExecutionPlan | null>(inspectorKey, null);
  const [capabilityAdvisories, setCapabilityAdvisories] = useConversationState<CapabilityAdvisory[]>(inspectorKey, []);
  const [pendingFollowUp, setPendingFollowUp] = useConversationState<{
    actionType: string; purpose: string; evidence: string; expiresAt: string;
  } | null>(inspectorKey, null);
  const [budgetBusy, setBudgetBusy] = useConversationState(inspectorKey, false);
  const [routingRevision, setRoutingRevision] = useState(0);
  const [routingOptions, setRoutingOptions] = useConversationState<RoutingModelOption[]>(inspectorKey, []);
  const [runActivityLabel, setRunActivityLabel] = useConversationState<string | null>(inspectorKey, null);
  const pendingSendRef = useRef<{ signature: string; idempotencyKey: string } | null>(null);
  const editActionsRef = useRef({ begin: prepareMessageEdit, save: sendEditedMessage });
  editActionsRef.current = { begin: prepareMessageEdit, save: sendEditedMessage };
  const beginMessageEdit = useCallback(() => editActionsRef.current.begin(), []);
  const editMessage = useCallback((message: ChatMessage, content: string) => editActionsRef.current.save(message, content), []);
  useEffect(() => () => {
    submissionRef.current = null;
    sendScopeRef.current = "";
  }, []);
  const conversationId = conversation?.id;
  const conversationKind = conversation?.kind;
  const activeProject = projects.find((item) => item.id === (conversation ? conversation.project_id : draft.kind === "project_work" ? draft.projectId : null));
  const webSearch = useWebSearchController(conversation, activeProject);
  const activityVersion = useMemo(() => `${changeSets.map((item) => `${item.id}-${item.status}-${item.undo_state}`).join(",")}:${toolOperations.map((item) => `${item.id}-${item.status}`).join(",")}:${webOperations.map((item) => `${item.id}-${item.status}-${item.result_count}`).join(",")}`,
    [changeSets, toolOperations, webOperations]);
  const scrollContentVersion = `${messages.length}:${streamingContent.length}:${sending}:${activityVersion}`;
  const conversationScroll = useConversationScroll(inspectorKey, scrollContentVersion, forceBottomVersion);

  useEffect(() => {
    if (pendingSearchSetupRevision === null
      || (webSearch.connectionRevision <= pendingSearchSetupRevision.connection
        && webSearch.permissionRevision <= pendingSearchSetupRevision.permission)) return;
    setPendingSearchSetupRevision(null);
    webSearch.setOpen(false);
    void send(input);
  }, [pendingSearchSetupRevision, webSearch.connectionRevision, webSearch.permissionRevision]);

  useEffect(() => {
    setPendingVisionConfirmation(null);
    setVisionConfirmationBusy(false);
    setPendingFollowUp(null);
    setFailedEditMessage(null);
    setKnowledgeProposal(null);
    setDeriveOpen(false);
    derivedProjectRef.current = null;
    derivedWorkRef.current = null;
    pendingSendRef.current = null;
    const submission = submissionRef.current;
    const adopted = adoptedScopeRef.current === inspectorKey;
    adoptedScopeRef.current = null;
    if (!adopted && (!submission || (submission.scope !== inspectorKey && submission.acceptedScope !== inspectorKey))) {
      submissionRef.current = null;
      runActionRef.current += 1;
      runIdRef.current = null;
      compactionResumeRef.current = false;
      setLastRunId(null);
      setConversation(null);
      setMessages([]);
      setBudgetBusy(false);
      setActiveRun(null);
      setSending(false);
      stoppingRef.current = false;
      setStopping(false);
      setInput("");
    }
  }, [inspectorKey]);

  useEffect(() => {
    if (currentPage !== "chat") return;
    let cancelled = false;
    const projectId = conversation ? conversation.project_id : (draft.kind === "project_work" ? draft.projectId : null);
    void Promise.all([
      settingsRepository.getAppSettings(),
      projectId ? projectRepository.getModelScope(projectId) : Promise.resolve(null),
    ]).then(([settings, scope]) => {
      if (cancelled) return;
      const project = projectId ? projects.find((item) => item.id === projectId) : undefined;
      setRoutingOptions(selectableModels(settings, project, scope, true).map((item) => ({
        value: item.value, providerId: item.provider.id, modelId: item.model.id,
        label: item.provider.name, unavailable: item.model.status === "unavailable",
      })));
    }).catch(() => { if (!cancelled) setRoutingOptions([]); });
    return () => { cancelled = true; };
  }, [conversation?.id, conversation?.project_id, draft.kind, draft.projectId, projects, currentPage, coreOnline, routingRevision]);

  useEffect(() => {
    if (!activeConversation) return;
    // Refresh metadata without replacing retained messages or generation state.
    setConversation(current => current?.id === activeConversation.id
      ? { ...current, ...activeConversation, messages: current.messages } : current);
  }, [activeConversation]);

  useEffect(() => {
    const returning = previousPageRef.current !== "chat" && currentPage === "chat";
    previousPageRef.current = currentPage;
    if (!returning || !activeConversationId || !coreOnline) return;
    let active = true;
    const metadataAtRequest = workspaceConversationRef.current;
    void conversationRepository.get(activeConversationId).then(latest => {
      if (active && metadataAtRequest === workspaceConversationRef.current) setConversation(current => current?.id === latest.id
        ? { ...current, ...latest, messages: current.messages } : current);
    }).catch(() => { if (active) { setError(true); setErrorDetail(tCommon("buttonAudit.actionFailed")); } });
    return () => { active = false; };
  }, [currentPage, activeConversationId, coreOnline]);

  useEffect(() => {
    toolAuditConversation.current = coreOnline ? activeConversationId : null;
    return () => {
      toolAuditConversation.current = null;
      toolAuditRequest.current += 1;
    };
  }, [activeConversationId, coreOnline]);

  const refreshToolOperations = useCallback(() => {
    const conversationId = activeConversationId;
    if (!conversationId || !coreOnline || toolAuditConversation.current !== conversationId) return;
    const request = ++toolAuditRequest.current;
    void agentExecutionRepository.listConversationOperations(conversationId).then((items) => {
      if (toolAuditConversation.current === conversationId && toolAuditRequest.current === request) {
        setToolOperations(items);
      }
    }).catch(() => undefined);
  }, [activeConversationId, coreOnline]);

  const refreshChangeSets = useCallback(() => {
    refreshToolOperations();
    if (!activeConversationId) {
      setChangeSets([]);
      return;
    }
    void changeSetRepository.list().then((items) => {
      setChangeSets(items.filter((item) => item.conversation_id === activeConversationId));
    }).catch(() => undefined);
  }, [activeConversationId, refreshToolOperations]);

  const refreshWebOperations = useCallback(() => {
    if (!activeConversationId) {
      setWebOperations([]);
      return;
    }
    void webSearchRepository.listConversationOperations(activeConversationId)
      .then(setWebOperations).catch(() => undefined);
  }, [activeConversationId]);

  useEffect(() => {
    let unsubscribe: (() => void) | undefined;
    let active = true;
    void modelRunRepository.subscribe((event: ModelRunEvent) => {
      if (!active) return;
      if (event.event_type.startsWith("run.context_compaction_")) {
        const eventConversationId = String(event.data.conversation_id ?? "");
        if (eventConversationId && eventConversationId === activeConversationId) {
          void contextRepository.compactionStatus(eventConversationId).then(setCompaction).catch(() => undefined);
        }
      }
      if (event.run_id !== runIdRef.current) return;
      if (event.event_type === "run.chunk") {
        appendStreamingContent(String(event.data.delta ?? ""));
      } else if (event.event_type === "run.state_changed") {
        const status = event.data.status;
        if (isModelRunStatus(status)) {
          setActiveRun((current) => current ? { ...current, status } : current);
          if (["queued", "building_context", "generating", "waiting_tool"].includes(status)) {
            terminalRefreshRevision.current += 1;
            setSending(true);
          }
        }
      } else if (event.event_type === "run.plan_ready") {
        setRunActivityLabel(tChat("routing.status.planning"));
      } else if (event.event_type === "run.step_started") {
        const step = String(event.data.reason_code ?? "");
        const model = String(event.data.model_id ?? "");
        if (step === "vision_analysis") setRunActivityLabel(tChat("routing.status.vision", { model }));
        else if (step === "web_research") setRunActivityLabel(tChat("routing.status.web", { model }));
        else if (step === "reasoning_analysis") setRunActivityLabel(tChat("routing.status.reasoning", { model }));
      } else if (event.event_type === "run.synthesis_started") {
        setRunActivityLabel(tChat("routing.status.synthesis", { model: String(event.data.model_id ?? "") }));
      } else if (event.event_type === "run.invocation_retrying") {
        setRunActivityLabel(tChat("routing.status.retrying", {
          retry: Number(event.data.retry_number ?? 1),
          max: Number(event.data.max_retries ?? 2),
        }));
      } else if (event.event_type === "run.response_recovering") {
        setRunActivityLabel(tChat("status.recovering"));
      } else if (event.event_type === "run.intent_follow_up_pending") {
        setPendingFollowUp({
          actionType: String(event.data.action_type ?? "web_search"),
          purpose: String(event.data.purpose ?? ""),
          evidence: String(event.data.evidence ?? "required"),
          expiresAt: String(event.data.expires_at ?? ""),
        });
      } else if (event.event_type === "run.tool_completed") {
        const message = event.data.message as ChatMessage | undefined;
        if (message) {
          setMessages((current) => current.some((item) => item.id === message.id)
            ? current : [...current, message]);
        }
        refreshChangeSets();
      } else if (event.event_type === "run.completed") {
        const message = event.data.message as ChatMessage;
        const summary = event.data.execution_summary_message as ChatMessage | undefined;
        const progress = (event.data.progress_messages ?? []) as ChatMessage[];
        setMessages((current) => {
          const withProgress = [...current, ...progress.filter((item) => !current.some((existing) => existing.id === item.id))];
          const next = withProgress.some((item) => item.id === message.id) ? withProgress : [...withProgress, message];
          return summary && !next.some((item) => item.id === summary.id) ? [...next, summary] : next;
        });
        clearStreamingContent(); setActiveRun(null); runIdRef.current = null; setSending(false); setRunActivityLabel(null);
        stoppingRef.current = false; setStopping(false);
        void refreshWorkspace();
      } else if (event.event_type === "run.failed" || event.event_type === "run.cancelled") {
        const refreshRevision = ++terminalRefreshRevision.current;
        setActiveRun((current) => current ? applyRunFailure(current, event) : current);
        setError(event.event_type !== "run.cancelled"); setSending(false); setRunActivityLabel(null);
        stoppingRef.current = false; setStopping(false);
        void modelRunRepository.get(event.run_id).then((run) => {
          if (runIdRef.current !== event.run_id || terminalRefreshRevision.current !== refreshRevision) return;
          const failed = applyRunFailure(run, event);
          setActiveRun(failed); replaceStreamingContent(failed.draft?.content ?? "");
        }).catch(() => { /* Keep the explanatory result from the terminal event. */ });
      } else if (event.event_type === "run.waiting_context") {
        void modelRunRepository.get(event.run_id).then((run) => {
          setActiveRun(run); setError(false); setSending(false);
        });
      } else if (event.event_type === "run.waiting_budget") {
        void modelRunRepository.get(event.run_id).then(async (run) => {
          setActiveRun(run); setError(false); setSending(false);
          const planId = String(event.data.plan_id ?? run.execution_plan_id ?? "");
          if (planId) setPendingBudgetPlan(await modelRunRepository.getPlan(planId));
        }).catch((value) => {
          setError(true); setErrorDetail(value instanceof Error ? value.message : String(value));
        });
      }
      if (event.event_type.startsWith("run.tool_") || event.event_type === "run.waiting_approval" || event.event_type === "run.change_set_ready") {
        refreshToolOperations();
      }
      if (event.event_type.startsWith("run.web_search_") || event.event_type.startsWith("run.tool_")
          || event.event_type === "run.completed" || event.event_type === "run.failed") {
        refreshWebOperations();
      }
    }).then((dispose) => { if (active) unsubscribe = dispose; else dispose(); }).catch(() => { /* Browser test shell has no Tauri event plugin. */ });
    return () => { active = false; unsubscribe?.(); };
  }, [activeConversationId, appendStreamingContent, clearStreamingContent, refreshChangeSets,
    refreshToolOperations, refreshWebOperations, refreshWorkspace, replaceStreamingContent]);

  useEffect(() => {
    let unsubscribe: (() => void) | undefined;
    let active = true;
    void changeSetRepository.subscribe(() => {
      if (!active) return;
      refreshChangeSets();
    }).then((dispose) => { if (active) unsubscribe = dispose; else dispose(); }).catch(() => { /* Browser test shell. */ });
    return () => { active = false; unsubscribe?.(); };
  }, [refreshChangeSets]);

  useEffect(() => {
    let cancelled = false;
    terminalRefreshRevision.current += 1;
    toolAuditRequest.current += 1;
    setChangeSets([]);
    setToolOperations([]);
    setWebOperations([]);
    setErrorDetail(null);
    setCompaction(null);
    setPendingBudgetPlan(null);
    setCapabilityAdvisories([]);
    clearStreamingContent();
    setRunActivityLabel(null);
    if (!activeConversationId || !coreOnline) {
      setConversation(null);
      setMessages([]);
      setLoading(false);
      setSending(false);
      setError(false);
      setFailedContent(null);
      setInput("");
      setDeriveOpen(false);
      return;
    }
    void changeSetRepository.list().then((items) => {
      if (!cancelled) setChangeSets(items.filter((item) => item.conversation_id === activeConversationId));
    }).catch(() => undefined);
    void webSearchRepository.listConversationOperations(activeConversationId).then((items) => {
      if (!cancelled) setWebOperations(items);
    }).catch(() => undefined);
    refreshToolOperations();
    setLoading(true);
    conversationRepository.get(activeConversationId)
      .then((value) => {
        if (!cancelled) {
          const metadata = workspaceConversationRef.current;
          setConversation(metadata?.id === value.id ? { ...value, ...metadata, messages: value.messages } : value);
          setMessages(value.messages);
          void contextRepository.compactionStatus(value.id).then((state) => {
            if (!cancelled) setCompaction(state);
          }).catch(() => undefined);
          void modelRunRepository.latest(value.id).then((run) => {
            if (cancelled || !run) return;
            setLastRunId(run.id);
            if (run.status === "completed") return;
            setActiveRun(run); runIdRef.current = run.id;
            replaceStreamingContent(run.draft?.content ?? "");
            setSending(["queued", "building_context", "generating", "waiting_tool"].includes(run.status));
            setError(["failed", "cancelled", "interrupted"].includes(run.status));
            if (run.status === "waiting_budget" && run.execution_plan_id) {
              void modelRunRepository.getPlan(run.execution_plan_id).then((plan) => {
                if (!cancelled) setPendingBudgetPlan(plan);
              }).catch(() => undefined);
            }
          }).catch(() => { /* Older browser test adapters do not expose Stage 3 run commands. */ });
        }
      })
      .catch(() => { if (!cancelled) setError(true); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [activeConversationId, clearStreamingContent, coreOnline, draft.revision, refreshToolOperations, replaceStreamingContent]);

  const markDraftChanged = useUpdateDraft({ input, setInput, conversation, loading, attachmentController, references });

  useEffect(() => () => { inspectorRequest.current += 1; }, []);

  useEffect(() => {
    if (currentPage !== "chat") return;
    let active = true;
    const projectId = conversation ? conversation.project_id : (
      draft.kind === "project_work" ? draft.projectId : null
    );
    if (!projectId) {
      setGrant(null);
      return;
    }
    void workToolRepository.getProjectGrant(projectId).then(value => { if (active) setGrant(value); })
      .catch(() => { if (active) { setError(true); setErrorDetail(tCommon("buttonAudit.actionFailed")); } });
    return () => { active = false; };
  }, [conversation?.project_id, draft.kind, draft.projectId, currentPage]);

  async function send(content: string, editedMessage?: ChatMessage) {
    const sendConversation = conversation && activeConversation?.id === conversation.id
      ? { ...conversation, ...activeConversation } : conversation;
    const normalized = content.trim();
    const selectedReferences = editedMessage ? [] : references.selected.map((item) => item.reference);
    const stagedAttachments = editedMessage ? [] : attachmentController.attachments;
    const stagedRejections = editedMessage ? [] : attachmentController.rejectedAttachments;
    if ((!normalized && stagedAttachments.length === 0 && !editedMessage?.attachments?.length
      && stagedRejections.length === 0 && selectedReferences.length === 0)
      || !coreOnline || loading || sending || submissionRef.current || attachmentController.busy || compactionResumeRef.current
      || (conversation !== null && conversation.id !== activeConversationId)
      || compactionBlocksSend || (!editedMessage && references.requiresChoice)) return;
    const submission = { optimisticId: `pending-${crypto.randomUUID()}`, scope: inspectorKey,
      acceptedScope: undefined as string | undefined, accepted: false, stopRequested: false };
    submissionRef.current = submission;
    runActionRef.current += 1;
    runIdRef.current = null;
    setActiveRun(null);
    const isCurrent = () => submissionRef.current === submission
      && (sendScopeRef.current === submission.scope || sendScopeRef.current === submission.acceptedScope);
    const restoreRejected = () => {
      setMessages((current) => current.filter((item) => item.id !== submission.optimisticId));
      if (!editedMessage) setInput((current) => current || content);
      stoppingRef.current = false;
      setStopping(false);
    };
    if (!editedMessage) setInput("");
    if (!editedMessage) setMessages((current) => [...current, {
      id: submission.optimisticId, conversation_id: conversation?.id ?? "", role: "user",
      content: normalized, created_at: new Date().toISOString(), event_type: null, source_message_id: null,
      attachments: attachmentController.attachments,
    }]);
    setForceBottomVersion((value) => value + 1);
    setSending(true);
    setPendingFollowUp(null);
    setError(false);
    setErrorDetail(null);
    setFailedContent(null);
    setFailedEditMessage(null);
    setRunActivityLabel(tChat("routing.status.planning"));
    clearStreamingContent();
    try {
      let settings = await settingsRepository.getAppSettings();
      const projectId = sendConversation ? sendConversation.project_id : (draft.kind === "project_work" ? draft.projectId : null);
      const project = projectId ? projects.find((item) => item.id === projectId) : undefined;
      const scope = projectId ? await projectRepository.getModelScope(projectId) : null;
      let candidates = selectableModels(settings, project, scope);
      const automaticMode = sendConversation
        ? sendConversation.model_selection_mode === "automatic"
        : (settings.routing?.new_conversation_mode ?? "automatic") === "automatic";
      let selected = resolveSelectedModel(
        candidates,
        automaticMode
          ? sendConversation?.coordinator_provider_profile_id ?? settings.models.default_provider_profile_id
          : sendConversation?.provider_profile_id ?? settings.models.default_provider_profile_id,
        automaticMode
          ? sendConversation?.coordinator_model_id ?? settings.models.default_model_id
          : sendConversation?.model_id ?? settings.models.default_model_id,
      );
      if (!selected) throw new Error(tChat("errors.selectCompatibleModel"));
      if (!automaticMode && effectiveKind === "project_work" && selected.model.tool_mode === "unknown") {
        settings = await modelCatalogRepository.probeTools(selected.provider.id, selected.model.id);
        candidates = selectableModels(settings, project, scope);
        selected = resolveSelectedModel(candidates, selected.provider.id, selected.model.id);
        if (!selected) throw new Error(tChat("errors.selectCompatibleModel"));
      }
      await providerRepository.connect(selected.provider.id);
      if (!isCurrent()) return;
      const sendSignature = JSON.stringify({
        conversation: conversation?.id ?? inspectorKey,
        content: normalized,
        editedMessage: editedMessage?.id,
        provider: selected.provider.id,
        model: selected.model.id,
        mode: automaticMode ? "automatic" : "fixed",
        attachments: stagedAttachments.map((item) => item.id),
        attachmentReferences: selectedReferences,
        rejectedAttachments: stagedRejections.map((item) => item.receipt_id),
        web: webSearch.requestMode,
      });
      if (pendingSendRef.current?.signature !== sendSignature) {
        pendingSendRef.current = { signature: sendSignature, idempotencyKey: crypto.randomUUID() };
      }
      const started = await modelRunRepository.start({
        conversationId: conversation?.id, kind: conversation ? undefined : draft.kind,
        category: !conversation && draft.kind === "standard_chat" ? draft.category : undefined,
        chatGroupId: !conversation && draft.kind === "standard_chat" ? draft.chatGroupId ?? undefined : undefined,
        projectId: !conversation && draft.kind === "project_work" ? draft.projectId ?? undefined : undefined,
        content: normalized,
        editMessageId: editedMessage?.id,
        idempotencyKey: pendingSendRef.current.idempotencyKey,
        providerProfileId: selected.provider.id,
        modelId: selected.model.id,
        selection: automaticMode ? {
          mode: "automatic",
          provider_profile_id: selected.provider.id,
          model_id: selected.model.id,
          routing_policy: {
            preset: sendConversation?.routing_preset ?? settings.routing?.default_preset ?? "balanced",
            allow_online: project?.model_access_policy !== "local_only",
            max_extra_online_cost_usd: settings.routing?.max_extra_online_cost_usd ?? "0.20",
          },
        } : {
          mode: "fixed",
          provider_profile_id: selected.provider.id,
          model_id: selected.model.id,
          source: conversation ? "conversation_override" : "global_default",
        },
        attachmentStagingIds: stagedAttachments.map((item) => item.id),
        attachmentReferences: selectedReferences,
        attachmentRejectionStagingIds: stagedRejections.map((item) => item.receipt_id),
        webSearchRequestMode: webSearch.requestMode,
      });
      if (!isCurrent()) {
        if (started.status !== "preflight_rejected" && submission.stopRequested) {
          await modelRunRepository.cancel(started.run.id);
        }
        void refreshWorkspace();
        return;
      }
      if (started.status === "preflight_rejected") {
        restoreRejected();
        setError(true);
        setErrorDetail(tChat(`errors.${started.rejection.reason_code}`, {
          defaultValue: tChat("errors.no_compatible_execution_plan"),
        }));
        setSending(false);
        return;
      }
      submission.accepted = true;
      submission.acceptedScope = started.conversation.id;
      if (started.conversation.id !== inspectorKey) adoptedScopeRef.current = started.conversation.id;
      if (editedMessage) attachmentController.adoptScope(started.conversation.id);
      pendingSendRef.current = null;
      setCapabilityAdvisories(started.execution_plan?.degraded_capabilities ?? []);
      webSearch.clearAcceptedOneShot();
      let acceptedConversation = started.conversation;
      runIdRef.current = started.run.id; setLastRunId(started.run.id); setActiveRun(started.run); setConversation(acceptedConversation);
      setMessages((current) => {
        if (editedMessage) return acceptedConversation.messages;
        const saved = current.filter((item) => item.id !== submission.optimisticId);
        return saved.some((item) => item.id === started.user_message.id) ? saved : [...saved, started.user_message];
      });
      setForceBottomVersion((value) => value + 1);
      if (!editedMessage) {
        attachmentController.clearSent();
        references.clear();
      }
      if (started.run.status === "waiting_budget" && started.execution_plan) {
        setPendingBudgetPlan(started.execution_plan);
      }
      if (!conversation || editedMessage) await conversationCreated(acceptedConversation);
      if (!isCurrent()) return;
      if (submission.stopRequested) {
        await cancelGeneration(started.run.id);
        return;
      }
      if (!conversation && webSearch.mode === "auto" && acceptedConversation.web_search_mode !== "auto") {
        acceptedConversation = await conversationRepository.updateWebSearch(acceptedConversation.id, "auto");
        if (!isCurrent()) return;
        setConversation(acceptedConversation);
      }
      const current = await modelRunRepository.get(started.run.id);
      if (!isCurrent() || runIdRef.current !== started.run.id) return;
      if (current.status === "completed" && current.assistant_message) {
        setMessages((items) => items.some((item) => item.id === current.assistant_message?.id)
          ? items : [...items, current.assistant_message as ChatMessage]);
        setActiveRun(null); runIdRef.current = null; setSending(false);
      } else if (["failed", "cancelled", "interrupted"].includes(current.status)) {
        setActiveRun(current); replaceStreamingContent(current.draft?.content ?? ""); setError(true); setSending(false);
      } else if (current.status === "waiting_context") {
        setActiveRun(current); setSending(false);
      } else if (current.status === "waiting_budget") {
        setActiveRun(current); setSending(false);
        const planId = current.execution_plan_id ?? started.execution_plan?.id;
        if (planId) setPendingBudgetPlan(await modelRunRepository.getPlan(planId));
      }
      return true;
    } catch (value) {
      if (!isCurrent()) return;
      if (!submission.accepted) restoreRejected();
      const detail = value instanceof Error ? value.message : String(value ?? "");
      if (detail.includes("external_search_setup_required")
        || detail.includes("search_backend_not_configured")
        || detail.includes("project_web_consent_required")
        || detail.includes("project_web_access_denied")) {
        setError(false);
        setErrorDetail(null);
        setPendingSearchSetupRevision({
          connection: webSearch.connectionRevision,
          permission: webSearch.permissionRevision,
        });
        webSearch.setOpen(true);
        setSending(false);
        return;
      }
      setError(true);
      setErrorDetail(value instanceof Error ? value.message : null);
      setFailedContent(normalized);
      setFailedEditMessage(editedMessage ?? null);
      if (!submission.accepted) setSending(false);
    } finally {
      if (submissionRef.current === submission) submissionRef.current = null;
    }
  }

  async function prepareMessageEdit() {
    if (!coreOnline || stoppingRef.current) return false;
    if (sending || (activeRun && ["waiting_context", "waiting_permission", "waiting_budget"].includes(activeRun.status))) {
      const stopped = await stopGeneration();
      return sendScopeRef.current === inspectorKey && Boolean(stopped
        && ["completed", "cancelled", "failed", "interrupted"].includes(stopped.status));
    }
    return true;
  }

  async function sendEditedMessage(message: ChatMessage, content: string) {
    return send(content, message);
  }

  async function confirmVisionCapability() {
    const pending = pendingVisionConfirmation;
    if (!pending || visionConfirmationBusy) return;
    setVisionConfirmationBusy(true);
    setError(false);
    setErrorDetail(null);
    try {
      await settingsRepository.updateModelVisionConfirmation(
        pending.providerId, pending.modelId, true,
      );
      setPendingVisionConfirmation(null);
      await send(pending.content);
    } catch (value) {
      setError(true);
      setErrorDetail(value instanceof Error ? value.message : String(value));
    } finally {
      setVisionConfirmationBusy(false);
    }
  }

  async function pauseCompaction() {
    if (!conversation) return;
    try { setCompaction(await contextRepository.pauseCompaction(conversation.id)); }
    catch (value) { setError(true); setErrorDetail(value instanceof Error ? value.message : null); }
  }

  async function resumeCompaction(target?: CompactionTargetRequest) {
    if (!conversation || compactionResumeRef.current) return;
    compactionResumeRef.current = true;
    setError(false);
    setErrorDetail(null);
    try { setCompaction(await contextRepository.resumeCompaction(conversation.id, target)); }
    catch (value) {
      setError(true);
      setErrorDetail(value instanceof Error ? value.message : tChat("context.compactionPaused"));
    } finally {
      compactionResumeRef.current = false;
    }
  }

  async function cancelGeneration(runId: string) {
    const action = runActionRef.current;
    const scope = sendScopeRef.current;
    const isCurrent = () => runActionRef.current === action && sendScopeRef.current === scope;
    function applyStoppedRun(run: ModelRun) {
      if (!isCurrent() || (runIdRef.current !== runId && runIdRef.current !== null)) return;
      terminalRefreshRevision.current += 1;
      if (run.status === "completed" && run.assistant_message) {
        setMessages((current) => current.some((item) => item.id === run.assistant_message?.id)
          ? current : [...current, run.assistant_message as ChatMessage]);
        setActiveRun(null); runIdRef.current = null; clearStreamingContent();
      } else {
        setActiveRun(run); replaceStreamingContent(run.draft?.content ?? "");
      }
      setSending(false);
      setError(run.status === "failed");
      setRunActivityLabel(null);
      return run;
    }
    try {
      const run = await modelRunRepository.cancel(runId);
      return applyStoppedRun(run);
    } catch (value) {
      if (!isCurrent()) return;
      try {
        const current = await modelRunRepository.get(runId);
        if (["completed", "failed", "cancelled", "interrupted"].includes(current.status)) return applyStoppedRun(current);
      } catch { /* Keep the original cancellation failure and allow another stop. */ }
      if (!isCurrent()) return;
      setError(true);
      setErrorDetail(value instanceof Error ? value.message : String(value));
    } finally {
      if (isCurrent()) {
        stoppingRef.current = false;
        setStopping(false);
      }
    }
  }

  async function stopGeneration() {
    if (stoppingRef.current) return;
    const submission = submissionRef.current;
    if (!activeRun && !submission) return;
    stoppingRef.current = true;
    setStopping(true);
    if (submission) submission.stopRequested = true;
    if (runIdRef.current) return cancelGeneration(runIdRef.current);
  }

  async function retryRun() {
    if (!activeRun) return;
    terminalRefreshRevision.current += 1;
    runActionRef.current += 1;
    setError(false); setSending(true); clearStreamingContent(); runIdRef.current = activeRun.id;
    try { setActiveRun(await modelRunRepository.retry(activeRun.id)); }
    catch (value) {
      setError(true); setErrorDetail(value instanceof Error ? value.message : null); setSending(false);
    }
  }

  async function retryRunWithoutWeb() {
    if (!activeRun) return;
    terminalRefreshRevision.current += 1;
    const action = ++runActionRef.current;
    setError(false); setSending(true); clearStreamingContent();
    try {
      const run = await modelRunRepository.retry(activeRun.id, "default", "off");
      if (runActionRef.current !== action || sendScopeRef.current !== inspectorKey) return;
      runIdRef.current = run.id; setLastRunId(run.id); setActiveRun(run);
    } catch (value) {
      setError(true); setErrorDetail(value instanceof Error ? value.message : null); setSending(false);
    }
  }

  async function continueContext() {
    if (!activeRun || activeRun.status !== "waiting_context") return;
    runActionRef.current += 1;
    setError(false); setSending(true); runIdRef.current = activeRun.id;
    try { setActiveRun(await modelRunRepository.continueContext(activeRun.id)); }
    catch (value) {
      setError(true); setErrorDetail(value instanceof Error ? value.message : null); setSending(false);
    }
  }

  async function resolveRoutingBudget(approved: boolean) {
    if (!activeRun || !pendingBudgetPlan || budgetBusy) return;
    const action = ++runActionRef.current;
    setBudgetBusy(true);
    setError(false);
    setErrorDetail(null);
    try {
      const run = await modelRunRepository.resolveBudget(activeRun.id, pendingBudgetPlan.plan_hash, approved);
      if (runActionRef.current !== action || sendScopeRef.current !== inspectorKey) return;
      setPendingBudgetPlan(null);
      setActiveRun(run);
      if (approved) {
        runIdRef.current = run.id;
        setSending(true);
      } else {
        runIdRef.current = null;
        setSending(false);
      }
    } catch (value) {
      setError(true);
      setErrorDetail(value instanceof Error ? value.message : String(value));
      if (activeRun.execution_plan_id) {
        void modelRunRepository.getPlan(activeRun.execution_plan_id).then(setPendingBudgetPlan).catch(() => undefined);
      }
    } finally {
      setBudgetBusy(false);
    }
  }

  async function updateRouting(
    mode: "fixed" | "automatic", option?: RoutingModelOption,
  ) {
    if (!conversation || sending) return;
    setError(false);
    setErrorDetail(null);
    try {
      const updated = await conversationRepository.updateRouting(
        conversation.id, mode, conversation.routing_preset ?? "balanced",
        option?.providerId, option?.modelId,
      );
      setConversation(updated);
      await refreshWorkspace();
    } catch (value) {
      setError(true);
      setErrorDetail(value instanceof Error ? value.message : String(value));
    }
  }

  async function expandedRetry() {
    if (!lastRunId || sending) return;
    const action = ++runActionRef.current;
    setError(false); setSending(true); clearStreamingContent();
    try {
      const run = await modelRunRepository.retry(lastRunId, "expanded_retry");
      if (runActionRef.current !== action || sendScopeRef.current !== inspectorKey) return;
      runIdRef.current = run.id; setLastRunId(run.id); setActiveRun(run);
      setPendingBudgetPlan(null);
      if (run.status === "waiting_budget") {
        if (!run.execution_plan_id) throw new Error(tChat("errors.execution_plan_stale"));
        setPendingBudgetPlan(await modelRunRepository.getPlan(run.execution_plan_id));
        setSending(false);
      } else if (run.status === "waiting_context") {
        setSending(false);
      }
      inspectorStore.close(inspectorKey);
    } catch (value) {
      setError(true); setErrorDetail(value instanceof Error ? value.message : null); setSending(false);
    }
  }

  async function showContext() {
    if (!lastRunId) return;
    if (inspector.open && inspector.view?.kind === "context") {
      inspectorStore.close(inspectorKey);
      return;
    }
    if (inspector.view?.kind === "context" && inspector.view.runId === lastRunId && inspector.view.snapshot) {
      inspectorStore.update(inspectorKey, (current) => ({ ...current, open: true }));
      return;
    }
    const request = ++inspectorRequest.current;
    inspectorStore.update(inspectorKey, () => ({
      open: true,
      view: { kind: "context", runId: lastRunId, snapshot: null, loading: true, error: null },
    }));
    try {
      const snapshot = await contextRepository.preview(lastRunId);
      if (request !== inspectorRequest.current) return;
      inspectorStore.update(inspectorKey, () => ({
        open: true,
        view: { kind: "context", runId: lastRunId, snapshot, loading: false, error: null },
      }));
    } catch (value) {
      if (request !== inspectorRequest.current) return;
      inspectorStore.update(inspectorKey, () => ({
        open: true,
        view: {
          kind: "context", runId: lastRunId, snapshot: null, loading: false,
          error: value instanceof Error ? value.message : tChat("context.loadFailed"),
        },
      }));
    }
  }

  const openProjectFile = useCallback(async (target: ProjectFileTarget) => {
    if (!conversationId || conversationKind !== "project_work") return;
    try {
      const preview = await projectFilePreviewRepository.preview(conversationId, target.path);
      updateInspector(inspectorKey, () => ({
        open: true,
        view: {
          kind: "project_file", title: preview.path, path: preview.path,
          content: preview.content, dataUrl: preview.data_url, language: null,
          lineStart: target.lineStart, lineEnd: target.lineEnd,
        },
      }));
    } catch (value) {
      setError(true);
      setErrorDetail(value instanceof Error ? value.message : String(value));
    }
  }, [conversationId, conversationKind, inspectorKey, updateInspector]);

  const openProjectDirectory = useCallback(async (target: ProjectDirectoryTarget) => {
    if (!conversationId || conversationKind !== "project_work") return;
    try {
      await projectDirectoryRepository.open(conversationId, target.path);
    } catch (value) {
      setError(true);
      setErrorDetail(value instanceof Error ? value.message : String(value));
    }
  }, [conversationId, conversationKind]);

  const openAttachment = useCallback(async (attachment: MessageAttachment) => {
    if (!conversationId) return;
    try {
      const preview = await attachmentRepository.previewMessage(conversationId, attachment.id);
      updateInspector(inspectorKey, () => ({
        open: true,
        view: {
          kind: "attachment", title: preview.name, attachmentId: preview.id,
          content: preview.content, dataUrl: preview.data_url,
        },
      }));
    } catch (value) {
      setError(true);
      setErrorDetail(value instanceof Error ? value.message : String(value));
    }
  }, [conversationId, inspectorKey, updateInspector]);

  const closeInspector = useCallback(() => {
    inspectorRequest.current += 1;
    inspectorStore.close(inspectorKey);
  }, [inspectorKey, inspectorStore]);

  async function proposeKnowledge() {
    if (!conversation || conversation.kind !== "project_work") return;
    const source = [...messages].reverse().find((message) => message.role === "assistant");
    if (!source) return;
    const title = window.prompt(tChat("knowledge.titlePrompt"), conversationTitle)?.trim();
    if (!title) return;
    const content = window.prompt(tChat("knowledge.contentPrompt"), source.content)?.trim();
    if (!content) return;
    await knowledgeAction.run(async isCurrent => {
      const proposal = await modelRunRepository.proposeKnowledge(conversation.id, "summary", title, content, source.id);
      if (isCurrent()) setKnowledgeProposal({ ...proposal, title, content });
    });
  }

  async function publishKnowledge() {
    if (!knowledgeProposal) return;
    await knowledgeAction.run(async isCurrent => {
      await modelRunRepository.publishKnowledge(knowledgeProposal.id, knowledgeProposal.title, knowledgeProposal.content);
      if (isCurrent()) setKnowledgeProposal(null);
    });
  }

  async function rejectKnowledge() {
    if (!knowledgeProposal) return;
    await knowledgeAction.run(async isCurrent => {
      await modelRunRepository.rejectKnowledge(knowledgeProposal.id);
      if (isCurrent()) setKnowledgeProposal(null);
    });
  }

  async function derive(event: FormEvent) {
    event.preventDefault();
    if (!conversation || conversation.kind !== "standard_chat") return;
    await deriveAction.run(async isCurrent => {
      let projectId = deriveProject || (derivedProjectRef.current?.scope === inspectorKey ? derivedProjectRef.current.id : "");
      if (!projectId) {
        if (!newProjectName.trim()) return;
        const project = await projectRepository.create(newProjectName, newProjectCategory);
        if (!isCurrent()) return;
        derivedProjectRef.current = { scope: inspectorKey, id: project.id };
        projectId = project.id;
        await refreshWorkspace();
        if (!isCurrent()) return;
      }
      let workId = derivedWorkRef.current?.scope === inspectorKey ? derivedWorkRef.current.id : "";
      if (!workId) {
        const work = await conversationRepository.deriveWork(conversation.id, projectId);
        if (!isCurrent()) return;
        derivedWorkRef.current = { scope: inspectorKey, id: work.id };
        workId = work.id;
      }
      await refreshWorkspace();
      if (!isCurrent()) return;
      setDeriveOpen(false);
      derivedWorkRef.current = null; derivedProjectRef.current = null;
      openConversation(workId);
    });
  }

  const effectiveKind = conversation?.kind ?? draft.kind;
  const source = conversation?.derived_from_conversation_id ?? null;

  return (
    <div className="chat-page">
      {attachmentController.dragPhase !== "idle" ? (
        <div className={`chat-attachment-drop chat-attachment-drop--${attachmentController.dragPhase}`}
          role="status" aria-live="polite">
          <div>
            <Upload size={28} aria-hidden="true" />
            <strong>{attachmentController.dragPhase === "processing"
              ? tChat("attachments.drop.processing", { count: attachmentController.dragCount })
              : tChat("attachments.drop.title", { count: attachmentController.dragCount })}</strong>
            <span>{tChat("attachments.drop.hint")}</span>
          </div>
        </div>
      ) : null}
      <div className="chat-toolbar">
        {conversation ? (
          <Button icon={<Pencil size={15} />} onClick={() => { if (conversation) resourceActions.rename({ kind: "conversation", id: conversation.id }); }}>
            {tChat("rename.action")}
          </Button>
        ) : null}
        {effectiveKind === "standard_chat" && conversation ? (
          <Button icon={<GitBranch size={15} />} onClick={() => setDeriveOpen(true)}>
            {tChat("derive.action")}
          </Button>
        ) : null}
        {lastRunId ? <Button onClick={() => void showContext()}>{tChat("context.details")}</Button> : null}
        {conversation?.kind === "project_work" && messages.some((message) => message.role === "assistant")
          ? <Button disabled={knowledgeAction.busy} onClick={() => void proposeKnowledge()}>{tChat("knowledge.propose")}</Button> : null}
      </div>
      {knowledgeAction.error ? <p className="form-error" role="alert">{knowledgeAction.error}</p> : null}

      {knowledgeProposal ? <section className="approval-card">
        <strong>{tChat("knowledge.review")}</strong><h3>{knowledgeProposal.title}</h3><p>{knowledgeProposal.content}</p>
        <div><Button disabled={knowledgeAction.busy} onClick={() => void rejectKnowledge()}>{tCommon("actions.cancel")}</Button>
          <Button disabled={knowledgeAction.busy} variant="primary" onClick={() => void publishKnowledge()}>{tChat("knowledge.publish")}</Button></div>
      </section> : null}

      <div className="chat-scroll-region">
        <div ref={conversationScroll.scrollRef} className="chat-messages" onScroll={conversationScroll.onScroll}>
          <div ref={conversationScroll.contentRef} className="chat-messages__content" aria-live="polite">
        {!loading && messages.length === 0 ? (
          <div className="chat-welcome">
            <div className="chat-welcome__avatar"><Bot size={32} /></div>
            <h2>{effectiveKind === "standard_chat" ? tChat("welcome.title") : tChat("workWelcome.title")}</h2>
            <p>{coreOnline
              ? (effectiveKind === "standard_chat" ? tChat("welcome.description") : tChat("workWelcome.description"))
              : tChat("status.offline")}</p>
          </div>
        ) : null}
        {loading ? <p className="state-message">{tChat("status.loading")}</p> : null}
        <ConversationTimeline messages={messages} changeSets={changeSets} operations={toolOperations}
          webOperations={webOperations}
          projectName={projects.find((item) => item.id === conversation?.project_id)?.name}
          conversationTitle={conversationTitle}
          onProjectFile={effectiveKind === "project_work" ? openProjectFile : undefined}
          onProjectDirectory={effectiveKind === "project_work" ? openProjectDirectory : undefined}
          onAttachment={openAttachment}
          onReferenceAttachment={references.choose}
          referenceDisabled={sending}
          onBeginEdit={beginMessageEdit} onEdit={editMessage} editDisabled={!coreOnline || sending || stopping}
          onChangeSetResolved={refreshChangeSets} />
        {sending ? (
          <AssistantRunMessage status={activeRun?.status ?? "queued"} content={streamingContent}
            activityLabel={runActivityLabel}
            onProjectFile={effectiveKind === "project_work" ? openProjectFile : undefined}
            onProjectDirectory={effectiveKind === "project_work" ? openProjectDirectory : undefined} />
        ) : null}
        {!sending && activeRun && ["failed", "cancelled", "interrupted"].includes(activeRun.status) ? (
          <div className="chat-message chat-message--assistant chat-message--draft">
            <span>{tChat("status.incomplete")}</span>
            <strong>{activeRun.error_code ? tChat(runErrorTranslationKey(activeRun.error_code, activeRun.error_message), { defaultValue: activeRun.error_message ?? activeRun.error_code }) : ""}</strong>
            {activeRun.draft?.content ? <WorkProcessDisclosure content={activeRun.draft.content} state="draft"
              onProjectFile={effectiveKind === "project_work" ? openProjectFile : undefined}
              onProjectDirectory={effectiveKind === "project_work" ? openProjectDirectory : undefined} /> : null}
            {activeRun.error_code === "invocation_route_stale" ? null
              : activeRun.web_search_route === "native" && [
              "image_format_unsupported", "multimodal_route_unsupported", "multimodal_protocol_unsupported",
            ].includes(activeRun.error_code ?? "") ? (
              <Button variant="ghost" icon={<RotateCcw size={14} />} onClick={() => void retryRunWithoutWeb()}>
                {tChat("attachments.retryWithoutWeb")}
              </Button>
            ) : (
              <Button variant="ghost" icon={<RotateCcw size={14} />} onClick={() => void retryRun()}>{tCommon("actions.retry")}</Button>
            )}
          </div>
        ) : null}
        {!sending && activeRun?.status === "waiting_context" ? (
          <div className="chat-message chat-message--assistant chat-message--context-waiting">
            <strong>{tChat("context.waitingTitle")}</strong>
            <p>{tChat("context.waitingDescription")}</p>
            <div><Button variant="ghost" type="button" onClick={() => void stopGeneration()}>{tCommon("actions.cancel")}</Button>
              <Button type="button" onClick={() => void continueContext()}>{tChat("context.continue")}</Button></div>
          </div>
        ) : null}
        {!sending && activeRun?.status === "waiting_budget" && pendingBudgetPlan ? (
          <RoutingBudgetPrompt plan={pendingBudgetPlan} busy={budgetBusy}
            onApprove={() => void resolveRoutingBudget(true)}
            onReject={() => void resolveRoutingBudget(false)} />
        ) : null}
        {capabilityAdvisories.length > 0 ? (
          <div className="chat-capability-notice" role="status">
            <strong>{tChat("capability.degradedTitle")}</strong>
            <p>{tChat("capability.degradedDescription")}</p>
            {capabilityAdvisories.map((item) => (
              <div key={`${item.capability_id}:${item.reason_code}`}>
                {item.capability_id === "vision_input"
                  ? <><strong>{tChat("capability.imageUnread")}</strong><p>{tChat("capability.imageAdvice")}</p></>
                  : <>{item.capability_id} · {tChat("capability.fallback", { mode: item.fallback_mode })}</>}
              </div>
            ))}
          </div>
        ) : null}
        {!sending && pendingFollowUp ? (
          <div className="chat-capability-notice" role="status">
            <strong>{tChat("followUp.pendingTitle")}</strong>
            <p>{tChat("followUp.pendingDescription", { purpose: pendingFollowUp.purpose })}</p>
            <small>{pendingFollowUp.evidence === "required"
              ? tChat("followUp.verifiedSourcesRequired")
              : tChat("followUp.verifiedSourcesOptional")}</small>
          </div>
        ) : null}
        {error ? (
          <div className="chat-error">{errorDetail ?? tChat(activeRun?.status === "failed" ? "status.responseInterrupted" : "status.error")}
            {failedContent ? (
              <Button variant="ghost" icon={<RotateCcw size={14} />} onClick={() => void send(failedContent, failedEditMessage ?? undefined)}>{tCommon("actions.retry")}</Button>
            ) : null}
          </div>
        ) : null}
          </div>
        </div>
        {conversationScroll.showBottomButton ? <button type="button" className="chat-scroll-to-bottom"
          aria-label={tChat("scroll.toBottom")} onClick={() => conversationScroll.scrollToBottom(true)}>
          <ArrowDown size={17} />
          {conversationScroll.hasNewContent ? <span aria-hidden="true" /> : null}
        </button> : null}
      </div>

      <ComposerDock
        value={input}
        disabled={!coreOnline}
        generating={sending || stopping}
        stopping={stopping}
        onStop={() => void stopGeneration()}
        sendDisabled={references.requiresChoice || compactionBlocksSend}
        referenceCount={references.selected.length}
        referenceTray={<AttachmentReferenceTray selected={references.selected} candidates={references.candidates}
          messages={messages} requiresChoice={references.requiresChoice} disabled={!coreOnline || sending}
          onChoose={references.choose} onRemove={references.remove} onDismissCandidates={references.dismissCandidates} />}
        compaction={compaction}
        onPauseCompaction={() => void pauseCompaction()}
        onResumeCompaction={(target) => void resumeCompaction(target)}
        compactionModels={routingOptions}
        conversation={conversation}
        project={projects.find((item) => item.id === conversation?.project_id)}
        grant={grant}
        isProjectWork={effectiveKind === "project_work"}
        derivedSourceId={source}
        onChange={(value) => { markDraftChanged(); setInput(value); }}
        onSend={() => void send(input)}
        onOpenSource={openConversation}
        onGrantChange={setGrant}
        onConversationChange={(updated) => { setConversation(updated); void refreshWorkspace(); }}
        attachments={attachmentController.attachments}
        rejectedAttachments={attachmentController.rejectedAttachments}
        attachmentPreviews={attachmentController.previews}
        attachmentBusy={attachmentController.busy}
        attachmentError={attachmentController.error}
        onAttach={() => { markDraftChanged(); void attachmentController.select(); }}
        onPasteImages={(files) => { markDraftChanged(); void attachmentController.pasteImages(files); }}
        onRemoveAttachment={(id) => void attachmentController.remove(id)}
        onRemoveRejectedAttachment={(id) => void attachmentController.removeRejected(id)}
        webSearch={webSearch}
        onOpenProjectSettings={activeProject ? () => openProject(activeProject.id) : undefined}
        inlineNotice={pendingVisionConfirmation ? <VisionCapabilityPrompt
          modelId={pendingVisionConfirmation.modelId} busy={visionConfirmationBusy}
          onCancel={() => setPendingVisionConfirmation(null)}
          onConfirm={() => void confirmVisionCapability()} /> : conversation?.copied_from_conversation_id ? (
            <button className="chat-original-version" type="button" onClick={() => openConversation(conversation.copied_from_conversation_id as string)}>
              <GitBranch size={14} />{tChat("messages.original")}
            </button>
          ) : undefined}
        routingControl={<ConversationRoutingControl conversation={conversation} options={routingOptions}
          onOpen={() => setRoutingRevision(value => value + 1)} onLoad={() => setCurrentPage("models")}
          disabled={sending || budgetBusy}
          onSelectAutomatic={() => void updateRouting("automatic")}
          onSelectFixed={(option) => void updateRouting("fixed", option)} />}
      />

      <ChatInspector entry={inspector} onClose={closeInspector} retrying={sending} referenceDisabled={sending}
        onExpandedRetry={expandedRetry} onReferenceAttachment={(id, range) => {
          const attachment = messages.flatMap((message) => message.attachments ?? []).find((item) => item.id === id);
          if (attachment) {
            references.choose(attachment, range);
            closeInspector();
          }
        }} />

      {deriveOpen ? (
        <div className="drawer-backdrop" onMouseDown={() => { if (!deriveAction.busy) setDeriveOpen(false); }}>
          <aside className="project-drawer" role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}>
            <div className="drawer-header">
              <h2>{tChat("derive.title")}</h2>
              <button disabled={deriveAction.busy} aria-label={tCommon("actions.close")} onClick={() => setDeriveOpen(false)}><X size={20} /></button>
            </div>
            <p>{tChat("derive.description")}</p>
            <form onSubmit={(event) => void derive(event)}>
              <label>{tChat("derive.project")}
                <select value={deriveProject} disabled={deriveAction.busy || derivedProjectRef.current?.scope === inspectorKey} onChange={(event) => setDeriveProject(event.target.value)}>
                  <option value="">{tChat("derive.newProject")}</option>
                  {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
                </select>
              </label>
              {!deriveProject ? (
                <>
                  <label>{tCommon("projects.drawer.name")}
                    <input value={newProjectName} disabled={deriveAction.busy || derivedProjectRef.current?.scope === inspectorKey} maxLength={80} required onChange={(event) => setNewProjectName(event.target.value)} />
                  </label>
                  <label>{tCommon("projects.drawer.category")}
                    <select value={newProjectCategory} disabled={deriveAction.busy || derivedProjectRef.current?.scope === inspectorKey} onChange={(event) => setNewProjectCategory(event.target.value as ProjectCategory)}>
                      {PROJECT_CATEGORIES.map((item) => (
                        <option key={item} value={item}>{tCommon(`projects.categories.${item}`)}</option>
                      ))}
                    </select>
                  </label>
                </>
              ) : null}
              {deriveAction.error ? <p className="form-error" role="alert">{deriveAction.error}</p> : null}
              {deriveAction.error && derivedProjectRef.current?.scope === inspectorKey ? <p role="status">{tCommon("buttonAudit.projectCreatedContinue")}</p> : null}
              <Button disabled={deriveAction.busy} type="submit" variant="primary">{tChat("derive.confirm")}</Button>
            </form>
          </aside>
        </div>
      ) : null}
    </div>
  );
}
