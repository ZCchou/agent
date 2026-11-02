import { invoke } from "@tauri-apps/api/core";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import type { AttachmentRepository } from "../repositories/AttachmentRepository";
import type {
  AttachmentDragStateEvent, AttachmentDropImportedEvent, AttachmentImportBatchResult,
  AttachmentNormalizationStatus, AttachmentPreview,
} from "../../types/domain";
import { nativeDropState, type NativeAttachmentDropEvent } from "../../features/chat/attachmentDrop";

const dragStateListeners = new Set<(event: AttachmentDragStateEvent) => void>();
const droppedListeners = new Set<(event: AttachmentDropImportedEvent) => void>();
let activeDropTarget: string | null = null;
let nativeUnlisten: (() => void) | undefined;
let nativeSetup = false;

function batchId() {
  return globalThis.crypto?.randomUUID?.() ?? `drop-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function ensureNativeDropListener() {
  if (nativeSetup) return;
  nativeSetup = true;
  void getCurrentWebview().onDragDropEvent(({ payload }) => {
    const targetId = activeDropTarget;
    if (!targetId) return;
    const event = payload as NativeAttachmentDropEvent;
    if (event.type === "over") return;
    const id = event.type === "drop" ? batchId() : null;
    const state = nativeDropState(targetId, event, id);
    for (const listener of dragStateListeners) listener(state);
    if (event.type !== "drop") return;
    void invoke<AttachmentImportBatchResult>("import_dropped_attachments", { paths: event.paths })
      .then((result) => {
        const dropped: AttachmentDropImportedEvent = {
          target_id: targetId, batch_id: id as string, result, error_code: null,
        };
        for (const listener of droppedListeners) listener(dropped);
      })
      .catch((value) => {
        const dropped: AttachmentDropImportedEvent = {
          target_id: targetId, batch_id: id as string, result: null,
          error_code: value instanceof Error ? value.message : String(value),
        };
        for (const listener of droppedListeners) listener(dropped);
      });
  }).then((unlisten) => {
    if (dragStateListeners.size || droppedListeners.size) nativeUnlisten = unlisten;
    else { nativeSetup = false; unlisten(); }
  }).catch(() => { nativeSetup = false; });
}

function releaseNativeDropListener() {
  if (dragStateListeners.size || droppedListeners.size) return;
  nativeUnlisten?.();
  nativeUnlisten = undefined;
  nativeSetup = false;
}

export class TauriAttachmentRepository implements AttachmentRepository {
  select(): Promise<AttachmentImportBatchResult> {
    return invoke("select_message_attachments");
  }
  async importImage(file: File): Promise<AttachmentImportBatchResult> {
    if (file.size > 10 * 1024 * 1024) throw new Error("attachment_too_large");
    const extensions: Record<string, string> = { "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif" };
    const extension = extensions[file.type];
    if (!extension) throw new Error("attachment_type_unsupported");
    const name = /\.(png|jpe?g|webp|gif)$/i.test(file.name) ? file.name : `pasted-image.${extension}`;
    const dataBase64 = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = reader.onabort = () => reject(new Error("attachment_processing_failed"));
      reader.onload = () => {
        const data = typeof reader.result === "string" ? reader.result.split(",")[1] : undefined;
        if (data) resolve(data);
        else reject(new Error("attachment_processing_failed"));
      };
      reader.readAsDataURL(file);
    });
    return invoke("import_clipboard_image", { name, dataBase64 });
  }
  setDropTarget(targetId: string, enabled: boolean): Promise<void> {
    activeDropTarget = enabled ? targetId : activeDropTarget === targetId ? null : activeDropTarget;
    return Promise.resolve();
  }
  subscribeDragState(listener: (event: AttachmentDragStateEvent) => void): Promise<() => void> {
    dragStateListeners.add(listener);
    ensureNativeDropListener();
    return Promise.resolve(() => {
      dragStateListeners.delete(listener);
      releaseNativeDropListener();
    });
  }
  subscribeDropped(listener: (event: AttachmentDropImportedEvent) => void): Promise<() => void> {
    droppedListeners.add(listener);
    ensureNativeDropListener();
    return Promise.resolve(() => {
      droppedListeners.delete(listener);
      releaseNativeDropListener();
    });
  }
  discard(attachmentId: string): Promise<void> {
    return invoke("discard_staged_attachment", { attachmentId });
  }
  preview(attachmentId: string): Promise<AttachmentPreview> {
    return invoke("preview_staged_attachment", { attachmentId });
  }
  previewMessage(conversationId: string, attachmentId: string): Promise<AttachmentPreview> {
    return invoke("preview_message_attachment", { conversationId, attachmentId });
  }
  getNormalizationStatus(
    conversationId: string, attachmentId: string,
  ): Promise<AttachmentNormalizationStatus> {
    return invoke("get_attachment_normalization_status", { conversationId, attachmentId });
  }
}
