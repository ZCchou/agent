import { expect, it, vi } from "vitest";
import { loadRagPageData, readRagPageData } from "./ragPageData";

function source() {
  return {
    listModels: vi.fn().mockResolvedValue({ models: [], root: "models", runtime: {} }),
    listLibraries: vi.fn().mockResolvedValue({ libraries: [] }),
    listJobs: vi.fn().mockResolvedValue({ jobs: [] }),
  };
}

it("keeps a completed snapshot available when returning to the same project", async () => {
  const repository = source();
  expect(readRagPageData(repository, "project")).toBeNull();
  const snapshot = await loadRagPageData(repository, "project");
  expect(readRagPageData(repository, "project")).toBe(snapshot);
  expect(readRagPageData(repository, "another")).toBeNull();
  expect(readRagPageData(source(), "project")).toBeNull();
});

it("loads current project bindings without retaining another project's bindings", async () => {
  const repository = source();
  const library = { id: "library", name: "Rules", documents: [], generation: 1,
    embedding_model_id: "e", reranker_model_id: "r", active_index_id: "new-index" };
  repository.listLibraries.mockImplementation(async (project?: string) => ({ libraries: project
    ? [{ ...library, enabled: 1, index_id: "chosen-index" }] : [library] }));
  const snapshot = await loadRagPageData(repository, "project");
  expect(snapshot.libraries[0].project_ids).toEqual(["project"]);
  expect(snapshot.libraries[0].bound_index_id).toBe("chosen-index");
});

it("an older background response cannot overwrite newer page data", async () => {
  const repository = source();
  let resolve!: (value: { models: never[]; root: string; runtime: object }) => void;
  repository.listModels.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
  const older = loadRagPageData(repository, "old-project");
  const current = await loadRagPageData(repository, "current-project");
  resolve({ models: [], root: "old", runtime: {} });
  await older;
  expect(readRagPageData(repository, "current-project")).toBe(current);
  expect(readRagPageData(repository, "old-project")).toBeNull();
});

it("a failed refresh preserves the last displayed snapshot", async () => {
  const repository = source();
  const snapshot = await loadRagPageData(repository, "project");
  repository.listModels.mockRejectedValueOnce(new Error("temporarily unavailable"));
  await expect(loadRagPageData(repository, "project")).rejects.toThrow("temporarily unavailable");
  expect(readRagPageData(repository, "project")).toBe(snapshot);
});
