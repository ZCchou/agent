import { invoke } from "@tauri-apps/api/core";
import type { ProjectRepository } from "../repositories/ProjectRepository";
import type { Project, ProjectCategory, ProjectDeleteImpact, ProjectModelRef, ProjectModelScope } from "../../types/domain";

export class TauriProjectRepository implements ProjectRepository {
  list(): Promise<Project[]> { return invoke<Project[]>("list_projects"); }
  create(name: string, category: ProjectCategory): Promise<Project> {
    return invoke<Project>("create_project", { name, category });
  }
  update(projectId: string, name: string, category: ProjectCategory): Promise<Project> {
    return invoke<Project>("update_project", { projectId, name, category });
  }
  reorder(orderedIds: string[]): Promise<Project[]> {
    return invoke<Project[]>("reorder_projects", { orderedIds });
  }
  delete(projectId: string): Promise<void> { return invoke("delete_project", { projectId }); }
  deleteImpact(projectId: string): Promise<ProjectDeleteImpact> {
    return invoke("project_delete_impact", { projectId });
  }
  updateModelPolicy(projectId: string, modelAccessPolicy: "local_only" | "allow_online"): Promise<Project> {
    return invoke("update_project_model_policy", { projectId, modelAccessPolicy });
  }
  getModelScope(projectId: string): Promise<ProjectModelScope> {
    return invoke("get_project_model_scope", { projectId });
  }
  updateModelScope(projectId: string, policy: "inherit_global" | "restricted", models: ProjectModelRef[]): Promise<ProjectModelScope> {
    return invoke("update_project_model_scope", { projectId, policy, models });
  }
  async updateWebAccess(projectId: string, policy: "disabled" | "allow_search"): Promise<Project> {
    return (await invoke<{ project: Project }>("update_project_web_access", { projectId, policy })).project;
  }
}
