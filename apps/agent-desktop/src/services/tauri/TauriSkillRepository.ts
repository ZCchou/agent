import { invoke } from "@tauri-apps/api/core";
import type { SkillRepository } from "../repositories/SkillRepository";
import type { Skill, SkillDetail, SkillDraft } from "../../types/skills";
export class TauriSkillRepository implements SkillRepository {
  list() { return invoke<Skill[]>("list_skills"); }
  importDirectory() { return invoke<Skill | null>("import_skill_directory"); }
  inspect(name: string) { return invoke<SkillDetail>("inspect_skill", { name }); }
  setEnabled(skillId: string, enabled: boolean) { return invoke<Skill[]>("set_skill_enabled", { skillId, enabled }); }
  remove(skillId: string) { return invoke<Skill[]>("remove_skill", { skillId }); }
  save(draft: SkillDraft) { return invoke<SkillDetail>("save_skill", { draft }); }
}
