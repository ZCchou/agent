import type { ProjectGrant } from "../../types/domain";

export interface WorkToolRepository {
  chooseProjectDirectory(projectId: string): Promise<ProjectGrant | null>;
  getProjectGrant(projectId: string): Promise<ProjectGrant | null>;
}
