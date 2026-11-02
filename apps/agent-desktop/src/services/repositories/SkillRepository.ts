import type { Skill, SkillDetail, SkillDraft } from "../../types/skills";
export interface SkillRepository {
  list(): Promise<Skill[]>;
  importDirectory(): Promise<Skill | null>;
  inspect(name: string): Promise<SkillDetail>;
  setEnabled(skillId: string, enabled: boolean): Promise<Skill[]>;
  remove(skillId: string): Promise<Skill[]>;
  save(draft: SkillDraft): Promise<SkillDetail>;
}
