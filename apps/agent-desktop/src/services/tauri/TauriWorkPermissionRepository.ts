import { invoke } from "@tauri-apps/api/core";
import type { ApprovalMode, WorkPermission } from "../../types/domain";
import type { WorkPermissionRepository } from "../repositories/WorkPermissionRepository";

export class TauriWorkPermissionRepository implements WorkPermissionRepository {
  get(conversationId: string): Promise<WorkPermission> {
    return invoke("get_work_permission", { conversationId });
  }

  set(conversationId: string, mode: ApprovalMode): Promise<WorkPermission> {
    return invoke("set_work_permission", { conversationId, mode });
  }
}
