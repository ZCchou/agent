import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { TauriProjectRepository } from "./TauriProjectRepository";

const invokeMock = vi.mocked(invoke);

describe("TauriProjectRepository", () => {
  beforeEach(() => invokeMock.mockReset());

  it("lists projects through the typed command", async () => {
    invokeMock.mockResolvedValue([]);
    expect(await new TauriProjectRepository().list()).toEqual([]);
    expect(invokeMock).toHaveBeenCalledWith("list_projects");
  });

  it("creates a project with stable values", async () => {
    const expected = { id: "p1", name: "Research", category: "research",
      created_at: "now", updated_at: "now" } as const;
    invokeMock.mockResolvedValue(expected);
    expect(await new TauriProjectRepository().create("Research", "research")).toEqual(expected);
    expect(invokeMock).toHaveBeenCalledWith("create_project", { name: "Research", category: "research" });
  });

  it("updates project behavior category", async () => {
    invokeMock.mockResolvedValue({ id: "p1" });
    await new TauriProjectRepository().update("p1", "Code", "coding");
    expect(invokeMock).toHaveBeenCalledWith("update_project", {
      projectId: "p1", name: "Code", category: "coding",
    });
  });

  it("persists project order", async () => {
    invokeMock.mockResolvedValue([]);
    await new TauriProjectRepository().reorder(["p2", "p1"]);
    expect(invokeMock).toHaveBeenCalledWith("reorder_projects", { orderedIds: ["p2", "p1"] });
  });

  it("loads delete impact before soft deletion", async () => {
    invokeMock.mockResolvedValue({ project_id: "p1", work_count: 2, message_count: 4 });
    const repository = new TauriProjectRepository();
    await repository.deleteImpact("p1");
    expect(invokeMock).toHaveBeenLastCalledWith("project_delete_impact", { projectId: "p1" });
    await repository.delete("p1");
    expect(invokeMock).toHaveBeenLastCalledWith("delete_project", { projectId: "p1" });
  });
});
