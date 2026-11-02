import type { AttachmentDragStateEvent } from "../../types/domain";

export type NativeAttachmentDropEvent =
  | { type: "enter"; paths: string[] }
  | { type: "over" }
  | { type: "drop"; paths: string[] }
  | { type: "leave" };

export function nativeDropState(
  targetId: string,
  event: NativeAttachmentDropEvent,
  batchId: string | null = null,
): AttachmentDragStateEvent {
  if (event.type === "enter") {
    return { target_id: targetId, phase: "enter", count: event.paths.length, batch_id: null };
  }
  if (event.type === "drop") {
    return { target_id: targetId, phase: "processing", count: event.paths.length, batch_id: batchId };
  }
  if (event.type === "leave") {
    return { target_id: targetId, phase: "leave", count: 0, batch_id: null };
  }
  return { target_id: targetId, phase: "enter", count: 0, batch_id: null };
}
