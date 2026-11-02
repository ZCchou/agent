import type { Project, ProjectCategory, ProjectDeleteImpact, ProjectModelRef, ProjectModelScope } from "../../types/domain";

export interface ProjectRepository {
  list(): Promise<Project[]>;
  create(name: string, category: ProjectCategory): Promise<Project>;
  update(id: string, name: string, category: ProjectCategory): Promise<Project>;
  reorder(orderedIds: string[]): Promise<Project[]>;
  delete(id: string): Promise<void>;
  deleteImpact(id: string): Promise<ProjectDeleteImpact>;
  updateModelPolicy(id: string, policy: "local_only" | "allow_online"): Promise<Project>;
  getModelScope(id: string): Promise<ProjectModelScope>;
  updateModelScope(id: string, policy: "inherit_global" | "restricted", models: ProjectModelRef[]): Promise<ProjectModelScope>;
  updateWebAccess(id: string, policy: "disabled" | "allow_search"): Promise<Project>;
}
