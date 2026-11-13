import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type { ModelRunRepository, StartedModelRun, StartModelRunInput } from "../repositories/ModelRunRepository";
import type { ContextSnapshot, ExecutionPlan, ModelRun, ModelRunEvent, ProjectKnowledge } from "../../types/domain";

import { intentTasks, type IntentTask } from "./intentTasks";

export class TauriModelRunRepository implements ModelRunRepository {
  async start(input: StartModelRunInput): Promise<StartedModelRun> {
    const scope = input.intentScope ?? input.conversationId ?? input.idempotencyKey ?? crypto.randomUUID();
    intentTasks.begin(scope);
    const response = await invoke<StartedModelRun | { task: IntentTask }>("start_model_run", { request: {
      intent_async: true, intent_scope: scope,
      conversation_id: input.conversationId ?? null, kind: input.kind ?? null,
      category: input.category ?? null, chat_group_id: input.chatGroupId ?? null,
      project_id: input.projectId ?? null, content: input.content,
      ...(input.ragMode ? { rag_mode: input.ragMode } : {}),
      ...(input.attachmentPurposes && Object.keys(input.attachmentPurposes).length ? { attachment_purposes: input.attachmentPurposes } : {}),
      ...(input.editMessageId ? { edit_message_id: input.editMessageId } : {}),
      provider_profile_id: input.providerProfileId, model_id: input.modelId,
      idempotency_key: input.idempotencyKey ?? null,
      attachment_staging_ids: input.attachmentStagingIds ?? [],
      ...(input.attachmentReferences?.length ? { attachment_references: input.attachmentReferences.map((reference) => ({
        attachment_id: reference.attachment_id,
        ...(reference.source_start !== undefined ? { source_start: reference.source_start } : {}),
        ...(reference.source_end !== undefined ? { source_end: reference.source_end } : {}),
      })) } : {}),
      attachment_rejection_staging_ids: input.attachmentRejectionStagingIds ?? [],
      ...(input.webSearchRequestMode ? { web_search_request_mode: input.webSearchRequestMode } : {}),
      ...(input.selection ? { selection: input.selection } : {}),
    } });
    return "task" in response ? intentTasks.wait(response.task) : response;
  }
  async get(runId: string): Promise<ModelRun> { return (await invoke<{ run: ModelRun }>("get_model_run", { runId })).run; }
  async latest(conversationId: string): Promise<ModelRun | null> {
    return (await invoke<{ run: ModelRun | null }>("get_conversation_model_run", { conversationId })).run;
  }
  async cancel(runId: string): Promise<ModelRun> { return (await invoke<{ run: ModelRun }>("cancel_model_run", { runId })).run; }
  async retry(
    runId: string,
    contextStrategy: "default" | "expanded_retry" = "default",
    webSearchOverride?: "off",
  ): Promise<ModelRun> {
    return (await invoke<{ run: ModelRun }>("retry_model_run", {
      runId, contextStrategy, webSearchOverride: webSearchOverride ?? null,
    })).run;
  }
  async continueContext(runId: string): Promise<ModelRun> {
    return (await invoke<{ run: ModelRun }>("continue_model_run_context", { runId })).run;
  }
  async resolveBudget(runId: string, planHash: string, approved: boolean): Promise<ModelRun> {
    return (await invoke<{ run: ModelRun }>("resolve_model_run_budget", {
      runId, planHash, approved,
    })).run;
  }
  async getPlan(planId: string): Promise<ExecutionPlan> {
    return (await invoke<{ plan: ExecutionPlan }>("get_model_run_plan", { planId })).plan;
  }
  async context(runId: string): Promise<ContextSnapshot | null> {
    return (await invoke<{ snapshot: ContextSnapshot | null }>("get_run_context", { runId })).snapshot;
  }
  subscribe(listener: (event: ModelRunEvent) => void): Promise<() => void> {
    return listen<ModelRunEvent>("model-run-event", (event) => listener(event.payload));
  }
  async listModels(providerProfileId: string) {
    return (await invoke<{ models: Array<{ id: string; name: string; location: "local" | "online" }> }>(
      "list_provider_models", { providerProfileId },
    )).models;
  }
  async listKnowledge(projectId: string, includeArchived = false): Promise<ProjectKnowledge[]> {
    return (await invoke<{ knowledge: ProjectKnowledge[] }>("list_project_knowledge", { projectId, includeArchived })).knowledge;
  }
  async proposeKnowledge(conversationId: string, knowledgeType: ProjectKnowledge["knowledge_type"], title: string, content: string, sourceMessageId?: string) {
    return (await invoke<{ proposal: { id: string } }>("propose_project_knowledge", {
      conversationId, knowledgeType, title, content, sourceMessageId: sourceMessageId ?? null,
    })).proposal;
  }
  async publishKnowledge(proposalId: string, title?: string, content?: string): Promise<ProjectKnowledge> {
    return (await invoke<{ knowledge: ProjectKnowledge }>("publish_project_knowledge", {
      proposalId, title: title ?? null, content: content ?? null,
    })).knowledge;
  }
  rejectKnowledge(proposalId: string): Promise<void> {
    return invoke("reject_project_knowledge", { proposalId });
  }
  async updateKnowledge(knowledgeId: string, title: string, content: string): Promise<ProjectKnowledge> {
    return (await invoke<{ knowledge: ProjectKnowledge }>("update_project_knowledge", { knowledgeId, title, content })).knowledge;
  }
  async pinKnowledge(knowledgeId: string, pinned: boolean): Promise<ProjectKnowledge> {
    return (await invoke<{ knowledge: ProjectKnowledge }>("pin_project_knowledge", { knowledgeId, pinned })).knowledge;
  }
  async archiveKnowledge(knowledgeId: string, archived: boolean): Promise<ProjectKnowledge> {
    return (await invoke<{ knowledge: ProjectKnowledge }>("archive_project_knowledge", { knowledgeId, archived })).knowledge;
  }
}
