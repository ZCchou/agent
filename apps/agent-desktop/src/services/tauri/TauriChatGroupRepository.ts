import { invoke } from "@tauri-apps/api/core";
import type { ChatGroupRepository } from "../repositories/ChatGroupRepository";
import type { ChatGroup } from "../../types/domain";

export class TauriChatGroupRepository implements ChatGroupRepository {
  list(): Promise<ChatGroup[]> {
    return invoke("list_chat_groups");
  }

  create(name: string): Promise<ChatGroup> {
    return invoke("create_chat_group", { name });
  }

  rename(chatGroupId: string, name: string): Promise<ChatGroup> {
    return invoke("rename_chat_group", { chatGroupId, name });
  }

  delete(chatGroupId: string): Promise<void> {
    return invoke("delete_chat_group", { chatGroupId });
  }

  reorder(orderedIds: string[]): Promise<ChatGroup[]> {
    return invoke("reorder_chat_groups", { orderedIds });
  }
}
