import { arrayMove } from "@dnd-kit/sortable";
import type { ChatGroup, Conversation, ConversationKind, Project } from "../../types/domain";

export type ResourceContainerKind = "ungrouped" | "chat_group" | "project";

export interface ConversationPlacement {
  kind: ConversationKind;
  containerId: string | null;
  targetIndex: number;
}

export function conversationsIn(
  conversations: Conversation[], kind: ConversationKind, containerId: string | null,
): Conversation[] {
  return conversations
    .filter((item) => item.kind === kind && (
      kind === "standard_chat" ? item.chat_group_id === containerId : item.project_id === containerId
    ))
    .sort((left, right) => left.sort_order - right.sort_order || left.id.localeCompare(right.id));
}

export function reorderEntities<T extends ChatGroup | Project>(
  entities: T[], activeId: string, overId: string,
): T[] {
  const from = entities.findIndex((item) => item.id === activeId);
  const to = entities.findIndex((item) => item.id === overId);
  if (from < 0 || to < 0 || from === to) return entities;
  return arrayMove(entities, from, to).map((item, sort_order) => ({ ...item, sort_order }));
}

export function placeConversation(
  conversations: Conversation[], conversationId: string, placement: ConversationPlacement,
): Conversation[] {
  const moving = conversations.find((item) => item.id === conversationId);
  if (!moving || moving.kind !== placement.kind) return conversations;
  const without = conversations.filter((item) => item.id !== conversationId);
  const destination = conversationsIn(without, placement.kind, placement.containerId);
  const targetIndex = Math.max(0, Math.min(placement.targetIndex, destination.length));
  destination.splice(targetIndex, 0, {
    ...moving,
    chat_group_id: placement.kind === "standard_chat" ? placement.containerId : null,
    project_id: placement.kind === "project_work" ? placement.containerId : null,
  });
  const destinationIds = new Set(destination.map((item) => item.id));
  const normalizedDestination = destination.map((item, sort_order) => ({ ...item, sort_order }));
  const sourceContainerId = moving.kind === "standard_chat" ? moving.chat_group_id : moving.project_id;
  const normalizedSource = conversationsIn(without, moving.kind, sourceContainerId)
    .map((item, sort_order) => ({ ...item, sort_order }));
  const replacements = new Map(
    [...normalizedDestination, ...normalizedSource].map((item) => [item.id, item]),
  );
  return without.map((item) => replacements.get(item.id) ?? item)
    .filter((item) => !destinationIds.has(item.id))
    .concat(normalizedDestination);
}

export function orderedConversationIds(
  conversations: Conversation[], kind: ConversationKind, containerId: string | null,
): string[] {
  return conversationsIn(conversations, kind, containerId).map((item) => item.id);
}
