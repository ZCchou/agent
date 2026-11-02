import type { AttachmentReference, ContextSnapshot, Conversation, ExecutionPlan, ModelRun, ModelRunEvent, ModelSelectionDecision, ModelSelectionRequest, ProjectKnowledge } from "../../types/domain";

export interface StartModelRunInput {
  conversationId?: string;
  kind?: "standard_chat" | "project_work";
  category?: string;
  chatGroupId?: string;
  projectId?: string;
  content: string;
  editMessageId?: string;
  providerProfileId: string;
  modelId: string;
  idempotencyKey?: string;
  selection?: ModelSelectionRequest;
  attachmentStagingIds?: string[];
  attachmentReferences?: AttachmentReference[];
  attachmentRejectionStagingIds?: string[];
  webSearchRequestMode?: "off" | "auto" | "force_once";
}
export interface AcceptedModelRun {
  status: "accepted" | "waiting_permission" | "waiting_budget";
  run: ModelRun;
  conversation: Conversation;
  user_message: import("../../types/domain").ChatMessage;
  selection_decision: ModelSelectionDecision;
  web_search_decision?: { route: "none" | "native" | "host_tool" | "host_presearch" | "specialist"; reason_code: string };
  execution_plan?: ExecutionPlan;
}
export interface PreflightRejectedModelRun {
  status: "preflight_rejected";
  rejection: {
    reason_code: string;
    phase: "requirements" | "selection" | "routing" | "preflight";
    unmet_capabilities: string[];
    attempted_topologies: string[];
    rejected_routes: Array<{
      provider_id: string; model_id: string; route_id: string; reason_code: string;
    }>;
    suggested_actions: string[];
  };
}
export type StartedModelRun = AcceptedModelRun | PreflightRejectedModelRun;
export interface ModelRunRepository {
  start(input: StartModelRunInput): Promise<StartedModelRun>;
  get(runId: string): Promise<ModelRun>;
  latest(conversationId: string): Promise<ModelRun | null>;
  cancel(runId: string): Promise<ModelRun>;
  retry(
    runId: string,
    contextStrategy?: "default" | "expanded_retry",
    webSearchOverride?: "off",
  ): Promise<ModelRun>;
  continueContext(runId: string): Promise<ModelRun>;
  resolveBudget(runId: string, planHash: string, approved: boolean): Promise<ModelRun>;
  getPlan(planId: string): Promise<ExecutionPlan>;
  context(runId: string): Promise<ContextSnapshot | null>;
  subscribe(listener: (event: ModelRunEvent) => void): Promise<() => void>;
  listModels(providerProfileId: string): Promise<Array<{ id: string; name: string; location: "local" | "online" }>>;
  listKnowledge(projectId: string, includeArchived?: boolean): Promise<ProjectKnowledge[]>;
  proposeKnowledge(conversationId: string, type: ProjectKnowledge["knowledge_type"], title: string, content: string, sourceMessageId?: string): Promise<{ id: string }>;
  publishKnowledge(proposalId: string, title?: string, content?: string): Promise<ProjectKnowledge>;
  rejectKnowledge(proposalId: string): Promise<void>;
  updateKnowledge(id: string, title: string, content: string): Promise<ProjectKnowledge>;
  pinKnowledge(id: string, pinned: boolean): Promise<ProjectKnowledge>;
  archiveKnowledge(id: string, archived: boolean): Promise<ProjectKnowledge>;
}
