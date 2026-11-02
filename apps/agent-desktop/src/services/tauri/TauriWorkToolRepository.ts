import { invoke } from "@tauri-apps/api/core";
import type { WorkToolRepository } from "../repositories/WorkToolRepository";
import type { ProjectGrant } from "../../types/domain";

export class TauriWorkToolRepository implements WorkToolRepository {
  chooseProjectDirectory(projectId: string): Promise<ProjectGrant | null> {
    return invoke("choose_project_directory", { projectId });
  }

  getProjectGrant(projectId: string): Promise<ProjectGrant | null> {
    return invoke("get_project_grant", { projectId });
  }

}
