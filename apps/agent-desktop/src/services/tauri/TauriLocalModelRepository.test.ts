import { expect, it, vi } from "vitest";
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { TauriLocalModelRepository } from "./TauriLocalModelRepository";
it("uses native file grants for inspect/import and independent root grants", async () => {
  const repo = new TauriLocalModelRepository();
  const selection = { grant_id: "native", path: "C:/models/main.gguf" };
  vi.mocked(invoke).mockResolvedValue(selection);
  await repo.selectPath("model"); await repo.inspect(selection); await repo.importModel(selection); await repo.setRoot(selection);
  expect(vi.mocked(invoke).mock.calls).toEqual([
    ["local_model_select_path", { kind: "model" }],
    ["local_model_request", { action: "model.inspect", params: selection }],
    ["local_model_request", { action: "model.import", params: selection }],
    ["local_model_request", { action: "storage.root", params: selection }],
  ]);
});
it("sends stable IDs and configuration without transport credentials", async () => {
  vi.mocked(invoke).mockClear();
  const repo = new TauriLocalModelRepository();
  await repo.configure("a".repeat(64), { device: "cuda", context_length: 8192, gpu_layers: -1, threads: 4, is_default: true });
  await repo.load("a".repeat(64)); await repo.cancel("job");
  expect(vi.mocked(invoke).mock.calls.map(call => call[1])).toEqual([
    { action: "model.configure", params: { model_id: "a".repeat(64), device: "cuda", context_length: 8192, gpu_layers: -1, threads: 4, is_default: true } },
    { action: "runtime.load", params: { model_id: "a".repeat(64) } },
    { action: "job.cancel", params: { job_id: "job" } },
  ]);
});
