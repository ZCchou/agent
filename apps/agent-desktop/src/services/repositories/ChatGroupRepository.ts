import type { ChatGroup } from "../../types/domain";

export interface ChatGroupRepository {
  list(): Promise<ChatGroup[]>;
  create(name: string): Promise<ChatGroup>;
  rename(id: string, name: string): Promise<ChatGroup>;
  delete(id: string): Promise<void>;
  reorder(orderedIds: string[]): Promise<ChatGroup[]>;
}
