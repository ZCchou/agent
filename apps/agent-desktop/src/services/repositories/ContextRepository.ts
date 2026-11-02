import type { CompactionTargetRequest, ContextCompactionState, ContextSnapshot, ContextSourcePreview } from "../../types/domain";

export interface ContextRepository {
  preview(runId: string): Promise<ContextSnapshot | null>;
  source(runId: string, itemId: string): Promise<ContextSourcePreview>;
  compactionStatus(conversationId: string): Promise<ContextCompactionState | null>;
  resumeCompaction(conversationId: string, target?: CompactionTargetRequest): Promise<ContextCompactionState | null>;
  pauseCompaction(conversationId: string): Promise<ContextCompactionState | null>;
}
