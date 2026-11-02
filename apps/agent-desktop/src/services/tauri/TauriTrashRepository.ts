import { invoke } from "@tauri-apps/api/core";
import type { TrashRepository } from "../repositories/TrashRepository";
import type { TrashEntityType, TrashItem } from "../../types/domain";

export class TauriTrashRepository implements TrashRepository {
  list(): Promise<TrashItem[]> { return invoke("list_trash"); }
  restore(entityType: TrashEntityType, entityId: string): Promise<void> {
    return invoke("restore_trash", { entityType, entityId });
  }
  deletePermanently(entityType: TrashEntityType, entityId: string): Promise<void> {
    return invoke("permanently_delete_trash", { entityType, entityId });
  }
  empty(): Promise<void> { return invoke("empty_trash"); }
}
