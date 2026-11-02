import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { TauriChatGroupRepository } from "./TauriChatGroupRepository";

const invokeMock = vi.mocked(invoke);

describe("TauriChatGroupRepository", () => {
  beforeEach(() => invokeMock.mockReset());

  it("uses typed group commands", async () => {
    invokeMock.mockResolvedValue(undefined);
    const repository = new TauriChatGroupRepository();
    await repository.create("Ideas");
    await repository.rename("g1", "Notes");
    await repository.delete("g1");
    await repository.reorder(["g2", "g1"]);
    expect(invokeMock.mock.calls).toEqual([
      ["create_chat_group", { name: "Ideas" }],
      ["rename_chat_group", { chatGroupId: "g1", name: "Notes" }],
      ["delete_chat_group", { chatGroupId: "g1" }],
      ["reorder_chat_groups", { orderedIds: ["g2", "g1"] }],
    ]);
  });
});
