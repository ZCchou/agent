import { invoke } from "@tauri-apps/api/core";
import type { CompactionTargetRequest, ContextCompactionState, ContextSnapshot, ContextSourcePreview } from "../../types/domain";
import type { ContextRepository } from "../repositories/ContextRepository";

export class TauriContextRepository implements ContextRepository {
  async preview(runId: string): Promise<ContextSnapshot | null> {
    return (await invoke<{ snapshot: ContextSnapshot | null }>("get_run_context", { runId })).snapshot;
  }

  async source(runId: string, itemId: string): Promise<ContextSourcePreview> {
    return (await invoke<{ source: ContextSourcePreview }>("get_context_source", { runId, itemId })).source;
  }

  async compactionStatus(conversationId: string): Promise<ContextCompactionState | null> {
    return (await invoke<{ compaction: ContextCompactionState | null }>("get_context_compaction_status", { conversationId })).compaction;
  }

  async resumeCompaction(conversationId: string, target?: CompactionTargetRequest): Promise<ContextCompactionState | null> {
    return (await invoke<{ compaction: ContextCompactionState | null }>("resume_context_compaction", {
      conversationId,
      ...(target ? { target: { provider_profile_id: target.provider_profile_id, model_id: target.model_id } } : {}),
    })).compaction;
  }

  async pauseCompaction(conversationId: string): Promise<ContextCompactionState | null> {
    return (await invoke<{ compaction: ContextCompactionState | null }>("pause_context_compaction", { conversationId })).compaction;
  }
}
