import type { ChatMessage, ModelRunEvent } from "../../types/domain";

export interface DocumentPublicText {
  runId: string;
  closed?: boolean;
  streams: Array<{ id: string; content: string; sequence: number }>;
}

export function appendDocumentPublicText(current: DocumentPublicText | null, event: ModelRunEvent,
  activeRunId: string | null): DocumentPublicText | null {
  if (event.run_id !== activeRunId) return current;
  const existing = current?.runId === event.run_id ? current : null;
  if (["run.failed", "run.cancelled", "run.interrupted", "run.completed"].includes(event.event_type))
    return { ...(existing ?? { runId: event.run_id, streams: [] }), closed: true };
  if (event.event_type === "run.state_changed" && event.data.status === "generating")
    return existing?.closed ? { ...existing, closed: false } : current;
  if (existing?.closed || event.event_type !== "run.document_processing"
    || event.data.phase !== "public_text" || event.data.public_text !== true
    || typeof event.data.stream_id !== "string" || typeof event.data.delta !== "string") return current;
  const next = existing ?? { runId: event.run_id, streams: [] };
  const old = next.streams.find(stream => stream.id === event.data.stream_id);
  if (old && old.sequence >= event.sequence) return current;
  const entry = { id: event.data.stream_id, content: (old?.content ?? "") + event.data.delta, sequence: event.sequence };
  return { ...next, streams: old ? next.streams.map(stream => stream === old ? entry : stream) : [...next.streams, entry] };
}

// Compare only with the persisted assistant message supplied for this exact run.
// Matching distinct sections in publication order prevents one occurrence from
// retiring two previews that happen to contain the same words.
export function reconcileDocumentReceipt(current: DocumentPublicText | null, runId: string, conversationId: string | null,
  message: ChatMessage | null | undefined): DocumentPublicText | null {
  if (current?.runId !== runId || !conversationId || !message?.id || message.role !== "assistant"
    || message.conversation_id !== conversationId || !message.content) return current;
  let offset = 0;
  const streams = current.streams.filter(stream => {
    const section = stream.content.trim();
    if (!section) return false;
    const found = message.content.indexOf(section, offset);
    if (found < 0) return true;
    offset = found + section.length;
    return false;
  });
  return { ...current, closed: true, streams };
}

type Publish = (update: (current: DocumentPublicText | null) => DocumentPublicText | null) => void;

// The normal chat stream also renders at most once per 60 ms. Keep all deltas
// and flush before terminal transitions, including a receipt obtained by IPC.
export function createDocumentTextBuffer(publish: Publish) {
  let pending: ModelRunEvent[] = [];
  let timer: ReturnType<typeof setTimeout> | undefined;
  function flush() {
    clearTimeout(timer);
    timer = undefined;
    if (!pending.length) return;
    const events = pending;
    pending = [];
    publish(current => events.reduce((state, event) => appendDocumentPublicText(state, event, event.run_id), current));
  }
  return {
    event(event: ModelRunEvent, activeRunId: string | null) {
      if (event.run_id !== activeRunId) return;
      if (event.event_type === "run.document_processing") {
        if (event.data.phase !== "public_text" || event.data.public_text !== true) return;
        pending.push(event);
        timer ??= setTimeout(flush, 60);
      } else if (event.event_type === "run.state_changed" || ["run.completed", "run.failed", "run.cancelled", "run.interrupted"].includes(event.event_type)) {
        pending.push(event);
        flush();
      }
    },
    receipt(runId: string, conversationId: string | null, message: ChatMessage | null | undefined) {
      flush();
      publish(current => reconcileDocumentReceipt(current, runId, conversationId, message));
    },
    dispose() {
      clearTimeout(timer);
      timer = undefined;
      pending = [];
    },
  };
}
