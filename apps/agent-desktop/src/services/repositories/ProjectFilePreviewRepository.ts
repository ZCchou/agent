import type { ProjectFilePreview } from "../../types/domain";

export interface ProjectFilePreviewRepository {
  preview(conversationId: string, path: string): Promise<ProjectFilePreview>;
  openDocument?(conversationId: string, path: string): Promise<void>;
}
