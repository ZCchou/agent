import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { TauriTrashRepository } from "./TauriTrashRepository";

const invokeMock = vi.mocked(invoke);

describe("TauriTrashRepository", () => {
  beforeEach(() => invokeMock.mockReset());

  it("uses typed trash lifecycle commands", async () => {
    invokeMock.mockResolvedValue([]);
    const repository = new TauriTrashRepository();
    await repository.list();
    expect(invokeMock).toHaveBeenLastCalledWith("list_trash");
    await repository.restore("project", "p1");
    expect(invokeMock).toHaveBeenLastCalledWith("restore_trash", { entityType: "project", entityId: "p1" });
    await repository.deletePermanently("conversation", "c1");
    expect(invokeMock).toHaveBeenLastCalledWith("permanently_delete_trash", { entityType: "conversation", entityId: "c1" });
    await repository.empty();
    expect(invokeMock).toHaveBeenLastCalledWith("empty_trash");
  });
});
