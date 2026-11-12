import { invoke } from "@tauri-apps/api/core";
import type { ProjectFilePreviewRepository } from "../repositories/ProjectFilePreviewRepository";
import type { ProjectFilePreview } from "../../types/domain";

export class TauriProjectFilePreviewRepository implements ProjectFilePreviewRepository {
  openDocument(conversationId: string, path: string): Promise<void> {
    return invoke("open_project_document", { conversationId, path });
  }
  preview(conversationId: string, path: string): Promise<ProjectFilePreview> {
    return invoke("preview_project_file", { conversationId, path });
  }
}
