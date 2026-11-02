import type { ApprovalMode, WorkPermission } from "../../types/domain";

export interface WorkPermissionRepository {
  get(conversationId: string): Promise<WorkPermission>;
  set(conversationId: string, mode: ApprovalMode): Promise<WorkPermission>;
}
