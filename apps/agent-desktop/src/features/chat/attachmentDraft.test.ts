import { describe, expect, it } from "vitest";
import type { MessageAttachment, RejectedAttachment } from "../../types/domain";
import {
  MAX_DRAFT_ATTACHMENT_BYTES, mergeAttachmentBatch,
} from "./attachmentDraft";
import { nativeDropState } from "./attachmentDrop";

function attachment(id: string, sha256: string, byteSize = 1): MessageAttachment {
  return {
    id, kind: "text", name: `${id}.txt`, media_type: "text/plain",
    byte_size: byteSize, sha256, width: null, height: null,
  };
}

function documentAttachment(id: string, byteSize = 100 * 1024 * 1024): MessageAttachment {
  return { ...attachment(id, id, byteSize), name: `${id}.pdf`, media_type: "application/pdf", artifacts: [{
    artifact_id: id, kind: "document.source", processor_id: "builtin.document", processor_version: "office-document-v1",
    media_type: "application/pdf", byte_size: byteSize, sha256: id,
  }] };
}

function rejection(id: string): RejectedAttachment {
  return { receipt_id: id, display_name: `${id}.bin`, error_code: "attachment_type_unsupported" };
}

describe("mergeAttachmentBatch", () => {
  it("accepts two 100 MiB documents within the 200 MiB draft budget", () => {
    const result = mergeAttachmentBatch([], [], {
      accepted: [documentAttachment("one"), documentAttachment("two")],
      rejected: [],
    });
    expect(result.accepted).toHaveLength(2);
    expect(result.errorCode).toBeNull();
  });

  it("keeps the 25 MiB budget for images and plain text", () => {
    const files = ["one", "two", "three"].map((id) => ({ ...attachment(id, id, 10 * 1024 * 1024), kind: "image" as const }));
    const result = mergeAttachmentBatch([], [], { accepted: files, rejected: [] });
    expect(result.accepted).toHaveLength(2);
    expect(result.discardedIds).toEqual(["three"]);
    expect(result.errorCode).toBe("attachments_too_large");
  });

  it("widens the budget when the document is last in an import batch", () => {
    const files = ["one", "two", "three"].map((id) => attachment(id, id, 10 * 1024 * 1024));
    const result = mergeAttachmentBatch([], [], { accepted: [...files, documentAttachment("last")], rejected: [] });
    expect(result.accepted).toHaveLength(4);
    expect(result.errorCode).toBeNull();
  });

  it("keeps the wider budget across imports when a document is already staged", () => {
    const result = mergeAttachmentBatch([documentAttachment("existing")], [], {
      accepted: [attachment("new", "new", 10 * 1024 * 1024)], rejected: [],
    });
    expect(result.accepted).toHaveLength(1);
  });

  it("does not widen the budget for a document beyond the remaining attachment slots", () => {
    const current = Array.from({ length: 6 }, (_, index) => attachment(`a${index}`, `a${index}`, 4 * 1024 * 1024));
    const result = mergeAttachmentBatch(current, [], {
      accepted: [attachment("one", "one", 1024 * 1024), attachment("two", "two", 1024 * 1024), documentAttachment("overflow")],
      rejected: [],
    });
    expect(result.accepted).toHaveLength(1);
    expect(result.errorCode).toBe("attachments_too_large");
  });

  it("deduplicates new files by trusted content hash", () => {
    const result = mergeAttachmentBatch(
      [attachment("existing", "same")], [],
      { accepted: [attachment("duplicate", "same"), attachment("new", "new")], rejected: [] },
    );
    expect(result.attachments.map((item) => item.id)).toEqual(["existing", "new"]);
    expect(result.discardedIds).toEqual(["duplicate"]);
    expect(result.errorCode).toBe("attachment_duplicate");
  });

  it("enforces cumulative draft size across multiple imports", () => {
    const result = mergeAttachmentBatch(
      [attachment("existing", "a", MAX_DRAFT_ATTACHMENT_BYTES - 5)], [],
      { accepted: [attachment("too-large", "b", 6)], rejected: [] },
    );
    expect(result.attachments).toHaveLength(1);
    expect(result.discardedIds).toEqual(["too-large"]);
    expect(result.errorCode).toBe("attachments_too_large");
  });

  it("bounds accepted and rejected draft entries and returns every discarded staging id", () => {
    const current = Array.from({ length: 8 }, (_, index) => attachment(`a${index}`, `h${index}`));
    const rejected = Array.from({ length: 8 }, (_, index) => rejection(`r${index}`));
    const result = mergeAttachmentBatch(current, rejected, {
      accepted: [attachment("overflow", "overflow")], rejected: [rejection("rejected-overflow")],
    });
    expect(result.attachments).toHaveLength(8);
    expect(result.rejectedAttachments).toHaveLength(8);
    expect(result.discardedIds).toEqual(["overflow", "rejected-overflow"]);
    expect(result.errorCode).toBe("attachment_count_invalid");
  });
});

describe("native attachment drop events", () => {
  it("normalizes Tauri enter, leave, and drop events for the active scope", () => {
    expect(nativeDropState("conversation-1", { type: "enter", paths: ["a.txt", "b.png"] })).toEqual({
      target_id: "conversation-1", phase: "enter", count: 2, batch_id: null,
    });
    expect(nativeDropState("conversation-1", { type: "leave" })).toEqual({
      target_id: "conversation-1", phase: "leave", count: 0, batch_id: null,
    });
    expect(nativeDropState("conversation-1", { type: "drop", paths: ["a.txt"] }, "batch-1")).toEqual({
      target_id: "conversation-1", phase: "processing", count: 1, batch_id: "batch-1",
    });
  });
});
