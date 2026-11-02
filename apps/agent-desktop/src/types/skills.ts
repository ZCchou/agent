export interface Skill {
  id: string;
  name: string;
  description: string;
  path: string;
  enabled: boolean;
  implicit: boolean;
  error: string | null;
}
export interface SkillDetail extends Skill { content: string; revision: string; }
export interface SkillDraft { name: string; description: string; content: string; expected_revision?: string; }
