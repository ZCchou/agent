export interface ProjectDirectoryRepository {
  open(conversationId: string, path: string): Promise<void>;
  openRoot(projectId: string): Promise<void>;
}
