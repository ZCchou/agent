import { invoke } from "@tauri-apps/api/core";
import type { ProjectFilePreviewRepository } from "../repositories/ProjectFilePreviewRepository";
import type { ProjectFilePreview } from "../../types/domain";

export class TauriProjectFilePreviewRepository implements ProjectFilePreviewRepository {
  preview(conversationId: string, path: string): Promise<ProjectFilePreview> {
    return invoke("preview_project_file", { conversationId, path });
  }
}
