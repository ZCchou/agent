import { invoke } from "@tauri-apps/api/core";
import type { LocalModelRepository } from "../repositories/LocalModelRepository";
import type { LocalModel, LocalModelConfiguration, LocalModelJob, LocalModelList, LocalPathSelection, LocalRuntimeCapabilities, LocalRuntimeStatus } from "../../types/localModels";
function request<T>(action: string, params: object = {}): Promise<T> {
  return invoke<T>("local_model_request", { action, params });
}
export class TauriLocalModelRepository implements LocalModelRepository {
  selectPath(kind: "model" | "root") { return invoke<LocalPathSelection | null>("local_model_select_path", { kind }); }
  revoke(selection: LocalPathSelection) { return invoke<void>("local_model_revoke_path", { grantId: selection.grant_id }); }
  list() { return request<LocalModelList>("model.list"); }
  inspect(selection: LocalPathSelection) { return request<{ model: LocalModel }>("model.inspect", selection); }
  importModel(selection: LocalPathSelection) { return request<{ job: LocalModelJob }>("model.import", selection); }
  configure(modelId: string, configuration: LocalModelConfiguration) { return request<{ model: LocalModel }>("model.configure", { model_id: modelId, ...configuration }); }
  remove(modelId: string) { return request("model.remove", { model_id: modelId }); }
  setRoot(selection: LocalPathSelection) { return request("storage.root", selection); }
  capabilities(verify = true) { return request<LocalRuntimeCapabilities>("runtime.capabilities", { verify }); }
  load(modelId: string) { return request<{ job: LocalModelJob }>("runtime.load", { model_id: modelId }); }
  unload() { return request<LocalRuntimeStatus>("runtime.unload"); }
  status() { return request<LocalRuntimeStatus>("runtime.status"); }
  jobs() { return request<{ jobs: LocalModelJob[] }>("job.list"); }
  cancel(jobId: string) { return request<{ job: LocalModelJob }>("job.cancel", { job_id: jobId }); }
}
