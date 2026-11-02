import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

import { invoke } from "@tauri-apps/api/core";
import { TauriMemoryRepository } from "./TauriMemoryRepository";

const invokeMock = vi.mocked(invoke);

describe("TauriMemoryRepository", () => {
  beforeEach(() => invokeMock.mockReset());

  it("loads global instructions through the Core bridge", async () => {
    const expected = {
      content: "先给结论。", enabled: true, revision: 2,
      content_hash: "hash", default_version: "agent-default-v1",
      updated_at: "now",
    };
    invokeMock.mockResolvedValue({ global_instructions: expected });

    const result = await new TauriMemoryRepository().getGlobalInstructions();

    expect(invokeMock).toHaveBeenCalledWith("get_global_instructions");
    expect(result).toEqual(expected);
  });

  it("saves and resets global instructions explicitly", async () => {
    const expected = {
      content: "使用中文。", enabled: false, revision: 3,
      content_hash: "hash-3", default_version: "agent-default-v1",
      updated_at: "now",
    };
    invokeMock.mockResolvedValue({ global_instructions: expected });
    const repository = new TauriMemoryRepository();

    await repository.updateGlobalInstructions("使用中文。", false);
    expect(invokeMock).toHaveBeenLastCalledWith("update_global_instructions", {
      content: "使用中文。", enabled: false,
    });

    await repository.resetGlobalInstructions();
    expect(invokeMock).toHaveBeenLastCalledWith("reset_global_instructions");
  });
});
