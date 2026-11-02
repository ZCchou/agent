import { invoke } from "@tauri-apps/api/core";
import type { ProjectDirectoryRepository } from "../repositories/ProjectDirectoryRepository";

export class TauriProjectDirectoryRepository implements ProjectDirectoryRepository {
  openRoot(projectId: string): Promise<void> {
    return invoke("open_project_root_directory", { projectId });
  }
  open(conversationId: string, path: string): Promise<void> {
    return invoke("open_project_directory", { conversationId, path });
  }
}
