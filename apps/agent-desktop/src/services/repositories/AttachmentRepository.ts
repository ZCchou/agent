import type {
  AttachmentDragStateEvent, AttachmentDropImportedEvent, AttachmentImportBatchResult,
  AttachmentNormalizationStatus, AttachmentPreview,
} from "../../types/domain";

export interface AttachmentRepository {
  select(): Promise<AttachmentImportBatchResult>;
  importImage(file: File): Promise<AttachmentImportBatchResult>;
  setDropTarget(targetId: string, enabled: boolean): Promise<void>;
  subscribeDragState(listener: (event: AttachmentDragStateEvent) => void): Promise<() => void>;
  subscribeDropped(listener: (event: AttachmentDropImportedEvent) => void): Promise<() => void>;
  discard(attachmentId: string): Promise<void>;
  preview(attachmentId: string): Promise<AttachmentPreview>;
  previewMessage(conversationId: string, attachmentId: string): Promise<AttachmentPreview>;
  getNormalizationStatus(
    conversationId: string, attachmentId: string,
  ): Promise<AttachmentNormalizationStatus>;
}
