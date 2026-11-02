import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { TauriChangeSetRepository } from "./TauriChangeSetRepository";
import { TauriWorkPermissionRepository } from "./TauriWorkPermissionRepository";

const invokeMock = vi.mocked(invoke);

describe("Stage 4 repositories", () => {
  beforeEach(() => invokeMock.mockReset());

  it("keeps approval modes behind typed Host commands", async () => {
    invokeMock.mockResolvedValue({ mode: "open" });
    const repository = new TauriWorkPermissionRepository();
    await repository.set("work-1", "open");
    expect(invokeMock).toHaveBeenCalledWith("set_work_permission", {
      conversationId: "work-1", mode: "open",
    });
  });

  it("resolves, undoes and rechecks change sets through Rust", async () => {
    invokeMock.mockResolvedValue(undefined);
    const repository = new TauriChangeSetRepository();
    await repository.resolve("change-1", { approve: true, planId: "plan-1", planHash: "immutable-hash", approvedOperationIds: ["directory", "write"] });
    await repository.undo("change-1");
    await repository.recover("change-1");
    expect(invokeMock.mock.calls).toEqual([
      ["resolve_agent_change_set", {
        changeSetId: "change-1", approve: true, planId: "plan-1", planHash: "immutable-hash", approvedOperationIds: ["directory", "write"],
      }],
      ["undo_agent_change_set", { changeSetId: "change-1" }],
      ["recover_agent_change_set", { changeSetId: "change-1" }],
    ]);
  });
});
