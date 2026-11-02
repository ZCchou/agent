import { useCallback, useEffect, useRef, useState } from "react";
import { attachmentRepository } from "../../services/runtime";
import type {
  AttachmentDropImportedEvent, AttachmentImportBatchResult, AttachmentPreview,
  MessageAttachment, RejectedAttachment,
} from "../../types/domain";
import { mergeAttachmentBatch } from "./attachmentDraft";

type DragPhase = "idle" | "ready" | "processing";

export function useAttachmentController(scopeId: string, dropEnabled: boolean) {
  const [attachments, setAttachments] = useState<MessageAttachment[]>([]);
  const [rejectedAttachments, setRejectedAttachments] = useState<RejectedAttachment[]>([]);
  const [previews, setPreviews] = useState<Record<string, AttachmentPreview>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragPhase, setDragPhase] = useState<DragPhase>("idle");
  const [dragCount, setDragCount] = useState(0);
  const attachmentsRef = useRef<MessageAttachment[]>([]);
  const rejectedRef = useRef<RejectedAttachment[]>([]);
  const scopeRef = useRef(scopeId);
  const busyCountRef = useRef(0);
  const activeDropBatchesRef = useRef(new Set<string>());
  const scopeGenerationRef = useRef(0);

  const beginBusy = useCallback(() => {
    busyCountRef.current += 1;
    setBusy(true);
  }, []);

  const endBusy = useCallback(() => {
    busyCountRef.current = Math.max(0, busyCountRef.current - 1);
    setBusy(busyCountRef.current > 0);
  }, []);

  const discardMany = useCallback(async (ids: string[]) => {
    await Promise.all(ids.map((id) => attachmentRepository.discard(id).catch(() => undefined)));
  }, []);

  const loadPreviews = useCallback(async (accepted: MessageAttachment[]) => {
    const generation = scopeGenerationRef.current;
    await Promise.all(accepted.map(async (attachment) => {
      try {
        const preview = await attachmentRepository.preview(attachment.id);
        if (generation === scopeGenerationRef.current) setPreviews((current) => ({ ...current, [attachment.id]: preview }));
      } catch {
        // Metadata remains usable even if a non-essential local preview fails.
      }
    }));
  }, []);

  const applyImportBatch = useCallback(async (selected: AttachmentImportBatchResult) => {
    const current = attachmentsRef.current;
    const currentRejected = rejectedRef.current;
    const merged = mergeAttachmentBatch(current, currentRejected, selected);
    attachmentsRef.current = merged.attachments;
    rejectedRef.current = merged.rejectedAttachments;
    setAttachments(merged.attachments);
    setRejectedAttachments(merged.rejectedAttachments);
    setError(merged.errorCode);

    await Promise.all([discardMany(merged.discardedIds), loadPreviews(merged.accepted)]);
  }, [discardMany, loadPreviews]);

  const select = useCallback(async () => {
    beginBusy();
    setError(null);
    try {
      await applyImportBatch(await attachmentRepository.select());
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
    } finally {
      endBusy();
    }
  }, [applyImportBatch, beginBusy, endBusy]);

  const pasteImages = useCallback(async (files: File[]) => {
    const generation = scopeGenerationRef.current;
    let importError: string | null = null;
    beginBusy();
    setError(null);
    try {
      for (const file of files) {
        if (generation !== scopeGenerationRef.current) break;
        try {
          const result = await attachmentRepository.importImage(file);
          if (generation !== scopeGenerationRef.current) {
            await discardMany([...result.accepted.map((item) => item.id), ...result.rejected.map((item) => item.receipt_id)]);
            break;
          }
          await applyImportBatch(result);
        } catch (value) {
          importError ??= value instanceof Error ? value.message : String(value);
          if (generation === scopeGenerationRef.current) setError(importError);
        }
      }
    } finally {
      if (generation === scopeGenerationRef.current) {
        if (importError) setError(importError);
        endBusy();
      }
    }
  }, [applyImportBatch, beginBusy, discardMany, endBusy]);

  const finishDroppedBatch = useCallback(async (event: AttachmentDropImportedEvent) => {
    if (event.target_id !== scopeRef.current) {
      if (event.result) {
        await discardMany([
          ...event.result.accepted.map((item) => item.id),
          ...event.result.rejected.map((item) => item.receipt_id),
        ]);
      }
      return;
    }
    try {
      if (event.error_code) setError(event.error_code);
      else if (event.result) await applyImportBatch(event.result);
    } finally {
      if (activeDropBatchesRef.current.delete(event.batch_id)) endBusy();
      setDragPhase("idle");
      setDragCount(0);
    }
  }, [applyImportBatch, discardMany, endBusy]);

  useEffect(() => {
    let active = true;
    let stopDragState: (() => void) | undefined;
    let stopDropped: (() => void) | undefined;
    void Promise.all([
      attachmentRepository.subscribeDragState((event) => {
        if (!active || event.target_id !== scopeRef.current) return;
        if (event.phase === "enter") {
          setDragPhase("ready");
          setDragCount(event.count);
        } else if (event.phase === "leave") {
          setDragPhase("idle");
          setDragCount(0);
        } else {
          setDragPhase("processing");
          setDragCount(event.count);
          if (event.batch_id && !activeDropBatchesRef.current.has(event.batch_id)) {
            activeDropBatchesRef.current.add(event.batch_id);
            beginBusy();
          }
        }
      }),
      attachmentRepository.subscribeDropped((event) => {
        if (active) void finishDroppedBatch(event);
      }),
    ]).then(([dragState, dropped]) => {
      if (active) {
        stopDragState = dragState;
        stopDropped = dropped;
      } else {
        dragState();
        dropped();
      }
    }).catch((value) => {
      if (active) setError(value instanceof Error ? value.message : String(value));
    });
    return () => {
      active = false;
      stopDragState?.();
      stopDropped?.();
    };
  }, [beginBusy, finishDroppedBatch]);

  useEffect(() => {
    void attachmentRepository.setDropTarget(scopeId, dropEnabled).catch((value) => {
      setError(value instanceof Error ? value.message : String(value));
    });
    return () => { void attachmentRepository.setDropTarget(scopeId, false).catch(() => undefined); };
  }, [dropEnabled, scopeId]);

  useEffect(() => {
    if (scopeRef.current === scopeId) return;
    scopeGenerationRef.current += 1;
    const staleIds = [
      ...attachmentsRef.current.map((item) => item.id),
      ...rejectedRef.current.map((item) => item.receipt_id),
    ];
    scopeRef.current = scopeId;
    attachmentsRef.current = [];
    rejectedRef.current = [];
    activeDropBatchesRef.current.clear();
    busyCountRef.current = 0;
    setAttachments([]);
    setRejectedAttachments([]);
    setPreviews({});
    setBusy(false);
    setError(null);
    setDragPhase("idle");
    setDragCount(0);
    void discardMany(staleIds);
  }, [discardMany, scopeId]);

  useEffect(() => () => {
    scopeGenerationRef.current += 1;
    const stagedIds = [
      ...attachmentsRef.current.map((item) => item.id),
      ...rejectedRef.current.map((item) => item.receipt_id),
    ];
    if (stagedIds.length) void discardMany(stagedIds);
  }, [discardMany]);

  const remove = useCallback(async (attachmentId: string) => {
    try {
      await attachmentRepository.discard(attachmentId);
      const next = attachmentsRef.current.filter((item) => item.id !== attachmentId);
      attachmentsRef.current = next;
      setAttachments(next);
      setPreviews((current) => {
        const updated = { ...current };
        delete updated[attachmentId];
        return updated;
      });
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
    }
  }, []);

  const removeRejected = useCallback(async (receiptId: string) => {
    try {
      await attachmentRepository.discard(receiptId);
      const next = rejectedRef.current.filter((item) => item.receipt_id !== receiptId);
      rejectedRef.current = next;
      setRejectedAttachments(next);
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
    }
  }, []);

  const clearSent = useCallback(() => {
    attachmentsRef.current = [];
    rejectedRef.current = [];
    setAttachments([]);
    setRejectedAttachments([]);
    setPreviews({});
    setError(null);
  }, []);

  const restore = useCallback((accepted: MessageAttachment[], rejected: RejectedAttachment[]) => {
    attachmentsRef.current = accepted;
    rejectedRef.current = rejected;
    setAttachments(accepted); setRejectedAttachments(rejected); setPreviews({}); setError(null);
    void loadPreviews(accepted);
  }, [loadPreviews]);

  const adoptScope = useCallback((nextScopeId: string) => {
    // An accepted edit continues in a new conversation. Its unsent attachment
    // draft follows that deliberate move; ordinary navigation still discards it.
    scopeGenerationRef.current += 1;
    scopeRef.current = nextScopeId;
  }, []);

  return {
    attachments, rejectedAttachments, previews, busy, error, dragPhase, dragCount,
    select, pasteImages, remove, removeRejected, clearSent, adoptScope, restore,
  };
}
