import { beforeEach, expect, it, vi } from "vitest";
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { TauriRagRepository } from "./TauriRagRepository";

beforeEach(() => { vi.mocked(invoke).mockReset(); });
it("passes OCR options and a single target project with the native document grant", async () => {
  vi.mocked(invoke).mockResolvedValue({});
  const repository = new TauriRagRepository();
  await repository.importDocuments("library", { grant_id: "file", paths: ["C:/DO160.pdf"] }, {
    ocr_mode: "auto", device: "cpu", auto_index: true, auto_apply_project_id: "project",
  });
  expect(invoke).toHaveBeenCalledExactlyOnceWith("rag_request", { action: "document.import", params: {
    library_id: "library", grant_id: "file", paths: ["C:/DO160.pdf"], ocr_mode: "auto",
    device: "cpu", auto_index: true, auto_apply_project_id: "project",
  } });
});
it("sends the exact reviewed revision selection when building an index", async () => {
  vi.mocked(invoke).mockResolvedValue({});
  await new TauriRagRepository().buildIndex("library", ["revision-1", "revision-3"]);
  expect(invoke).toHaveBeenCalledExactlyOnceWith("rag_request", { action: "index.build", params: { library_id: "library", version_ids: ["revision-1", "revision-3"] } });
});
it("uses native selections and preserves their grant for inspection and import", async () => {
  const selection = { grant_id: "one-use", paths: ["C:\\models\\embedding"] };
  vi.mocked(invoke).mockResolvedValue(selection);
  const repository = new TauriRagRepository();
  expect(await repository.selectPaths("model")).toEqual(selection);
  await repository.inspectModel(selection, "bge-m3");
  await repository.importModel(selection, "bge-m3", "cpu");
  expect(vi.mocked(invoke).mock.calls).toEqual([
    ["rag_select_paths", { kind: "model", modelFile: false }],
    ["rag_request", { action: "model.inspect", params: { path: selection.paths[0], grant_id: "one-use", profile: "bge-m3" } }],
    ["rag_request", { action: "model.import", params: { path: selection.paths[0], grant_id: "one-use", profile: "bge-m3", device: "cpu" } }],
  ]);
});
it("keeps project binding, document review, jobs and evidence behind typed commands", async () => {
  vi.mocked(invoke).mockResolvedValue({});
  const repository = new TauriRagRepository();
  await repository.bindLibrary("library", "project", false);
  await repository.importDocuments("library", { grant_id: "documents", paths: ["C:\\standard.pdf"] });
  await repository.confirmDocument({ version_id: "v1", revision: "R2", source: "CAAC", document_number: "AC-1", title: "Title", warnings_acknowledged: true });
  await repository.cancelJob("job");
  await repository.previewEvidence("project", "evidence");
  expect(vi.mocked(invoke).mock.calls.map((call) => call[1])).toEqual([
    { action: "library.bind", params: { library_id: "library", project_id: "project", enabled: false } },
    { action: "document.import", params: { library_id: "library", grant_id: "documents", paths: ["C:\\standard.pdf"] } },
    { action: "document.confirm", params: { version_id: "v1", revision: "R2", source: "CAAC", document_number: "AC-1", title: "Title", warnings_acknowledged: true } },
    { action: "job.cancel", params: { job_id: "job" } },
    { action: "evidence.preview", params: { project_id: "project", evidence_id: "evidence", offset: 0, length: 8000 } },
  ]);
});
it("revokes cancelled selections without forwarding filesystem parameters", async () => {
  vi.mocked(invoke).mockResolvedValue(undefined);
  await new TauriRagRepository().revokeSelection({ grant_id: "cancelled", paths: ["C:/model"] });
  expect(invoke).toHaveBeenCalledExactlyOnceWith("rag_revoke_paths", { grantId: "cancelled" });
});
it("supports model, library, document, task and retrieval operations", async () => {
  vi.mocked(invoke).mockImplementation(async (_command, args) => {
    const action = (args as { action: string }).action;
    if (action === 'search.start') return { task: { id: 'search-root', status: 'running' } };
    if (action === 'search.status') return { task: { id: 'search-root', status: 'completed' },
      result: { evidence: [], generation: 1, index_ids: [] } };
    return {};
  });
  const repository = new TauriRagRepository();
  const selection = { grant_id: "root", paths: ["C:/models"] };
  const draft = { name: "Library", embedding_model_id: "embedding", reranker_model_id: "ranking" };
  await repository.listModels();
  await repository.setModelRoot(selection);
  await repository.setModelDevice("model", "cuda");
  await repository.probeModel("model");
  await repository.removeModel("model");
  await repository.listLibraries("project");
  await repository.createLibrary(draft);
  await repository.updateLibrary("library", draft);
  await repository.removeLibrary("library");
  await repository.pasteDocument("library", "Title", "Source text");
  await repository.previewDocument("version", 200, 200);
  await repository.withdrawDocument("version");
  await repository.listJobs();
  await repository.buildIndex("library");
  await repository.search("project", "query");
  const calls = vi.mocked(invoke).mock.calls.map((call) => call[1] as { action: string; params: object });
  expect(calls.map((call) => call.action)).toEqual(["model.list", "model.root", "model.device", "model.probe", "model.remove", "library.list", "library.create", "library.update", "library.remove", "document.paste", "document.preview", "document.withdraw", "job.list", "index.build", "search.start", "search.status"]);
  expect(calls[10].params).toEqual({ version_id: "version", offset: 200, limit: 200 });
});

it("returns a standalone task identity before waiting for search results", async () => {
  vi.mocked(invoke).mockReset().mockImplementation(async (_command, args) => {
    if ((args as { action: string }).action === 'search.start') return { task: { id: 'library-search', status: 'running' } };
    return { task: { id: 'library-search', status: 'completed' }, result: { evidence: [], generation: 1, index_ids: [] } };
  });
  const identities: string[] = [];
  await new TauriRagRepository().searchLibrary('library', '振动', { onTaskId: (id) => identities.push(id) });
  expect(identities).toEqual(['library-search']);
  expect(vi.mocked(invoke).mock.calls[0][1]).toMatchObject({ action: 'search.start',
    params: { search_action: 'library.search', library_id: 'library', query: '振动', idempotency_key: expect.any(String) } });
});

it("reuses the original request key after uncertain search admission", async () => {
  vi.mocked(invoke).mockReset().mockRejectedValueOnce(new Error('core_request_timeout'))
    .mockResolvedValueOnce({ task: { id: 'recovered-search', status: 'running' } })
    .mockResolvedValueOnce({ task: { id: 'recovered-search', status: 'completed' },
      result: { evidence: [], generation: 1, index_ids: [] } });
  await new TauriRagRepository().searchLibrary('uncertain-library', 'query');
  const calls = vi.mocked(invoke).mock.calls;
  expect(calls[0][1]).toEqual(calls[1][1]);
  expect(calls[2][1]).toEqual({ action: 'search.status', params: { execution_task_id: 'recovered-search' } });
});

it("surfaces invalidated saved results and starts a fresh request on the next search", async () => {
  vi.mocked(invoke).mockReset().mockResolvedValueOnce({ task: { id: 'stale-search', status: 'completed' } })
    .mockResolvedValueOnce({ task: { id: 'stale-search', status: 'completed' }, result: null,
      error: { code: 'rag_generation_changed', message: '资料范围已变化' } })
    .mockResolvedValueOnce({ task: { id: 'new-search', status: 'running' } })
    .mockResolvedValueOnce({ task: { id: 'new-search', status: 'completed' },
      result: { evidence: [], generation: 2, index_ids: [] } });
  const repository = new TauriRagRepository();
  await expect(repository.searchLibrary('stale-library', 'query')).rejects.toThrow('rag_generation_changed');
  await expect(repository.searchLibrary('stale-library', 'query')).resolves.toMatchObject({ generation: 2 });
  const calls = vi.mocked(invoke).mock.calls;
  expect((calls[0][1] as { params: { idempotency_key: string } }).params.idempotency_key)
    .not.toBe((calls[2][1] as { params: { idempotency_key: string } }).params.idempotency_key);
});
