import type { TrashEntityType, TrashItem } from "../../types/domain";

export interface TrashRepository {
  list(): Promise<TrashItem[]>;
  restore(entityType: TrashEntityType, id: string): Promise<void>;
  deletePermanently(entityType: TrashEntityType, id: string): Promise<void>;
  empty(): Promise<void>;
}
