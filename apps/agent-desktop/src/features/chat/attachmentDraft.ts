import type {
  AttachmentImportBatchResult, MessageAttachment, RejectedAttachment,
} from "../../types/domain";

export const MAX_DRAFT_ATTACHMENTS = 8;
export const MAX_DRAFT_REJECTIONS = 8;
export const MAX_DRAFT_ATTACHMENT_BYTES = 25 * 1024 * 1024;

export const MAX_DRAFT_DOCUMENT_TOTAL_BYTES = 200 * 1024 * 1024;

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
  const candidateHashes = new Set(current.map((item) => item.sha256));
  const candidates = selected.accepted.filter((item) => {
    if (candidateHashes.has(item.sha256)) return false;
    candidateHashes.add(item.sha256);
    return true;
  }).slice(0, Math.max(0, MAX_DRAFT_ATTACHMENTS - current.length));
  const hasDocument = [...current, ...candidates].some((item) =>
    item.artifacts?.some((artifact) => artifact.kind === "document.source"));
  const maxTotalBytes = hasDocument ? MAX_DRAFT_DOCUMENT_TOTAL_BYTES : MAX_DRAFT_ATTACHMENT_BYTES;
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
    } else if (totalBytes + attachment.byte_size > maxTotalBytes) {
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
export function defaultAttachmentPurpose(name: string): "template" | "reference" {
  return /\.docx$/i.test(name) && /模板|template/i.test(name) ? "template" : "reference";
}
