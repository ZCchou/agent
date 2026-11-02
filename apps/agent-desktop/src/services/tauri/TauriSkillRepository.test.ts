import { beforeEach, expect, it, vi } from "vitest";
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { TauriSkillRepository } from "./TauriSkillRepository";

beforeEach(() => vi.mocked(invoke).mockReset());
it("keeps folder selection and skill management behind host commands", async () => {
  vi.mocked(invoke).mockResolvedValue([]);
  const r = new TauriSkillRepository();
  await r.list();
  await r.importDirectory();
  await r.setEnabled("id", false);
  await r.inspect("lesson");
  await r.remove("id");
  expect(vi.mocked(invoke).mock.calls).toEqual([
    ["list_skills"], ["import_skill_directory"],
    ["set_skill_enabled", { skillId: "id", enabled: false }],
    ["inspect_skill", { name: "lesson" }], ["remove_skill", { skillId: "id" }],
  ]);
});
