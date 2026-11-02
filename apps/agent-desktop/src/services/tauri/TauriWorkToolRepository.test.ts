import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { TauriWorkToolRepository } from "./TauriWorkToolRepository";

const invokeMock = vi.mocked(invoke);

describe("TauriWorkToolRepository", () => {
  beforeEach(() => invokeMock.mockReset());

  it("keeps directory grants behind Rust commands", async () => {
    invokeMock.mockResolvedValue(null);
    const repository = new TauriWorkToolRepository();
    await repository.chooseProjectDirectory("p1");
    expect(invokeMock.mock.calls).toEqual([
      ["choose_project_directory", { projectId: "p1" }],
    ]);
  });
});
