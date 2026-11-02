import { describe, expect, it } from "vitest";
import type { Conversation } from "../../types/domain";
import { orderedConversationIds, placeConversation, reorderEntities } from "./treeOperations";

function conversation(
  id: string, kind: "standard_chat" | "project_work", containerId: string | null, sort_order: number,
): Conversation {
  return {
    id, kind, sort_order, title: id, category: kind === "standard_chat" ? "casual" : null,
    resolved_category: "casual", chat_group_id: kind === "standard_chat" ? containerId : null,
    project_id: kind === "project_work" ? containerId : null,
    derived_from_conversation_id: null, derived_through_message_id: null, derived_at: null,
    copied_from_conversation_id: null, copied_through_message_id: null, copied_at: null,
    created_at: "now", updated_at: "now", messages: [],
  };
}

describe("sidebar tree operations", () => {
  it("moves a chat between groups and normalizes both containers", () => {
    const items = [conversation("a", "standard_chat", "g1", 0),
      conversation("b", "standard_chat", "g1", 1), conversation("c", "standard_chat", "g2", 0)];
    const moved = placeConversation(items, "a", {
      kind: "standard_chat", containerId: "g2", targetIndex: 1,
    });
    expect(orderedConversationIds(moved, "standard_chat", "g1")).toEqual(["b"]);
    expect(orderedConversationIds(moved, "standard_chat", "g2")).toEqual(["c", "a"]);
    expect(moved.find((item) => item.id === "a")?.chat_group_id).toBe("g2");
  });

  it("does not allow placement to change conversation kind", () => {
    const items = [conversation("a", "standard_chat", null, 0)];
    expect(placeConversation(items, "a", {
      kind: "project_work", containerId: "p1", targetIndex: 0,
    })).toBe(items);
  });

  it("reorders containers with stable integer positions", () => {
    const groups = [
      { id: "g1", name: "One", sort_order: 0, created_at: "now", updated_at: "now" },
      { id: "g2", name: "Two", sort_order: 1, created_at: "now", updated_at: "now" },
    ];
    expect(reorderEntities(groups, "g1", "g2").map((item) => [item.id, item.sort_order]))
      .toEqual([["g2", 0], ["g1", 1]]);
  });
});
