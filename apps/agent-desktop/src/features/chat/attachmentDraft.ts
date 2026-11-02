import type {
  AttachmentImportBatchResult, MessageAttachment, RejectedAttachment,
} from "../../types/domain";

export const MAX_DRAFT_ATTACHMENTS = 8;
export const MAX_DRAFT_REJECTIONS = 8;
export const MAX_DRAFT_ATTACHMENT_BYTES = 25 * 1024 * 1024;

export interface AttachmentBatchMerge {
  attachments: MessageAttachment[];
  rejectedAttachments: RejectedAttachment[];
  accepted: MessageAttachment[];
  discardedIds: string[];
  errorCode: string | null;
}

export function mergeAttachmentBatch(
  current: MessageAttachment[],
  currentRejected: RejectedAttachment[],
  selected: AttachmentImportBatchResult,
): AttachmentBatchMerge {
  const seenHashes = new Set(current.map((item) => item.sha256));
  let totalBytes = current.reduce((total, item) => total + item.byte_size, 0);
  const accepted: MessageAttachment[] = [];
  const discardedIds: string[] = [];
  let errorCode: string | null = null;

  for (const attachment of selected.accepted) {
    if (seenHashes.has(attachment.sha256)) {
      discardedIds.push(attachment.id);
      errorCode ??= "attachment_duplicate";
    } else if (current.length + accepted.length >= MAX_DRAFT_ATTACHMENTS) {
      discardedIds.push(attachment.id);
      errorCode ??= "attachment_count_invalid";
    } else if (totalBytes + attachment.byte_size > MAX_DRAFT_ATTACHMENT_BYTES) {
      discardedIds.push(attachment.id);
      errorCode ??= "attachments_too_large";
    } else {
      accepted.push(attachment);
      seenHashes.add(attachment.sha256);
      totalBytes += attachment.byte_size;
    }
  }

  const rejectionCapacity = Math.max(0, MAX_DRAFT_REJECTIONS - currentRejected.length);
  const retainedRejections = selected.rejected.slice(0, rejectionCapacity);
  discardedIds.push(...selected.rejected.slice(rejectionCapacity).map((item) => item.receipt_id));
  if (selected.rejected.length > rejectionCapacity) errorCode ??= "attachment_count_invalid";

  return {
    attachments: [...current, ...accepted],
    rejectedAttachments: [...currentRejected, ...retainedRejections],
    accepted,
    discardedIds,
    errorCode,
  };
}
