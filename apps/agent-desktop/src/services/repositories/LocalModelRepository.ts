import type { LocalModel, LocalModelConfiguration, LocalModelJob, LocalModelList, LocalPathSelection, LocalRuntimeCapabilities, LocalRuntimeStatus } from "../../types/localModels";
export interface LocalModelRepository {
  selectPath(kind: "model" | "root"): Promise<LocalPathSelection | null>;
  revoke(selection: LocalPathSelection): Promise<void>;
  list(): Promise<LocalModelList>;
  inspect(selection: LocalPathSelection): Promise<{ model: LocalModel }>;
  importModel(selection: LocalPathSelection): Promise<{ job: LocalModelJob }>;
  configure(modelId: string, configuration: LocalModelConfiguration): Promise<{ model: LocalModel }>;
  remove(modelId: string): Promise<unknown>;
  setRoot(selection: LocalPathSelection): Promise<unknown>;
  capabilities(verify?: boolean): Promise<LocalRuntimeCapabilities>;
  load(modelId: string): Promise<{ job: LocalModelJob }>;
  unload(): Promise<LocalRuntimeStatus>;
  status(): Promise<LocalRuntimeStatus>;
  jobs(): Promise<{ jobs: LocalModelJob[] }>;
  cancel(jobId: string): Promise<{ job: LocalModelJob }>;
}
