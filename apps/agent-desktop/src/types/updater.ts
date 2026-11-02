import type { ConversationKind, ProjectCategory, MessageAttachment, RejectedAttachment } from "./domain";
import type { SelectedAttachmentReference } from "../features/chat/useAttachmentReferences";
export type UpdateChannel = "preview" | "stable";
export interface UpdateView {
  state: "idle" | "checking" | "no_update" | "available" | "downloading" | "ready" | "installing" | "error" | "channel_unavailable";
  current_version: string; channel: UpdateChannel; update_id: string | null; version: string | null;
  notes: string | null; downloaded_bytes: number; total_bytes: number | null; error_code: string | null;
}
export interface RestartDraft {
  conversation_id: string | null; kind: ConversationKind; project_id: string | null; category: ProjectCategory;
  chat_group_id?: string | null;
  text: string; attachment_ids: string[]; rejected_ids: string[]; references: SelectedAttachmentReference[];
}
export interface RestoredUpdateDraft {
  update_id: string; draft: RestartDraft; attachments: MessageAttachment[];
  rejected_attachments: RejectedAttachment[]; missing_attachments: number;
}
