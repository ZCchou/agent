import type { RagDocument, RagFileInspection, RagTaskDetail } from "../../types/rag";
import { invoke } from "@tauri-apps/api/core";
import type { RagRepository } from "../repositories/RagRepository";
import type { OcrImportOptions, OcrCatalog, OcrPage, RagPathKind, RagPathSelection, RagProfile, RagModelList, RagLibrary, RagLibraryDraft, RagDocumentPreview, RagDocumentConfirmation, RagJob, RagSearchResult } from "../../types/rag";

function request<T = unknown>(action: string, params: object = {}): Promise<T> {
  return invoke<T>("rag_request", { action, params });
}
function pathParams(selection: RagPathSelection) {
  return { path: selection.paths[0], grant_id: selection.grant_id };
}
export class TauriRagRepository implements RagRepository {
  overview(projectId?: string) { return request<{ libraries: RagLibrary[] }>("library.overview", projectId ? { project_id: projectId } : {}); }
  documents(libraryId: string, offset = 0, query = "", includeWithdrawn = false) { return request<{ documents: RagDocument[]; total: number }>("document.list", { library_id: libraryId, offset, limit: 50, query, include_withdrawn: includeWithdrawn }); }
  history(documentId: string) { return request<{ versions: (RagDocument & { id: string })[] }>("document.history", { document_id: documentId }); }
  inspectDocuments(libraryId: string, selection: RagPathSelection) { return request<{ files: RagFileInspection[] }>("document.inspect", { library_id: libraryId, ...selection }); }
  taskDetail(jobId: string) { return request<RagTaskDetail>("job.detail", { job_id: jobId }); }
  resumeJob(jobId: string, acceptPartial = false) { return request("job.resume", { job_id: jobId, accept_partial: acceptPartial }); }
  searchLibrary(libraryId: string, query: string) { return request<RagSearchResult>("library.search", { library_id: libraryId, query }); }
  updateMetadata(confirmation: RagDocumentConfirmation) { return request("document.metadata", confirmation); }
  openOriginal(versionId: string) { return invoke<void>("rag_open_original", { versionId }); }
  selectPaths(kind: RagPathKind, modelFile = false) { return invoke<RagPathSelection | null>("rag_select_paths", { kind, modelFile }); }
  revokeSelection(selection: RagPathSelection) { return invoke<void>("rag_revoke_paths", { grantId: selection.grant_id }); }
  listModels() { return request<RagModelList>("model.list"); }
  inspectModel(selection: RagPathSelection, profile: RagProfile) { return request<Record<string, unknown>>("model.inspect", { ...pathParams(selection), profile }); }
  importModel(selection: RagPathSelection, profile: RagProfile, device: string) { return request("model.import", { ...pathParams(selection), profile, device }); }
  setModelRoot(selection: RagPathSelection) { return request("model.root", pathParams(selection)); }
  setModelDevice(modelId: string, device: string) { return request("model.device", { model_id: modelId, device }); }
  removeModel(modelId: string) { return request("model.remove", { model_id: modelId }); }
  probeModel(modelId: string) { return request("model.probe", { model_id: modelId }); }
  listLibraries(projectId?: string) { return request<{ libraries: RagLibrary[] }>("library.list", projectId ? { project_id: projectId } : {}); }
  createLibrary(draft: RagLibraryDraft) { return request("library.create", draft); }
  updateLibrary(libraryId: string, draft: RagLibraryDraft) { return request("library.update", { library_id: libraryId, ...draft }); }
  removeLibrary(libraryId: string) { return request("library.remove", { library_id: libraryId }); }
  bindLibrary(libraryId: string, projectId: string, enabled: boolean) { return request("library.bind", { library_id: libraryId, project_id: projectId, enabled }); }
  importDocuments(libraryId: string, selection: RagPathSelection, options?: OcrImportOptions) { return request("document.import", { library_id: libraryId, ...selection, ...options }); }
  listOcrModels() { return request<OcrCatalog>("ocr.model.list"); }
  importOcrModel(selection: RagPathSelection) { return request("ocr.model.import", pathParams(selection)); }
  ocrPages(versionId: string, page?: number) { return request<{ pages: OcrPage[]; summary: Record<string, unknown> }>("document.ocr.pages", { version_id: versionId, ...(page ? {page} : {}) }); }
  renderOcrPage(versionId: string, page: number) { return request<{ data_url: string }>("document.ocr.render", { version_id: versionId, page }); }
  pasteDocument(libraryId: string, title: string, content: string) { return request("document.paste", { library_id: libraryId, title, content }); }
  previewDocument(versionId: string, offset = 0, limit = 200, page?: number) { return request<RagDocumentPreview>("document.preview", { version_id: versionId, offset, limit, ...(page ? {page} : {}) }); }
  confirmDocument(confirmation: RagDocumentConfirmation) { return request("document.confirm", confirmation); }
  withdrawDocument(versionId: string) { return request("document.withdraw", { version_id: versionId }); }
  listJobs() { return request<{ jobs: RagJob[] }>("job.list"); }
  pauseJob(jobId: string) { return request("job.pause", { job_id: jobId }); }
  cancelJob(jobId: string) { return request("job.cancel", { job_id: jobId }); }
  buildIndex(libraryId: string, versionIds?: string[]) { return request("index.build", { library_id: libraryId, ...(versionIds ? { version_ids: versionIds } : {}) }); }
  search(projectId: string, query: string, libraryIds?: string[]) { return request<RagSearchResult>("search", { project_id: projectId, query, ...(libraryIds ? { library_ids: libraryIds } : {}) }); }
  previewEvidence(projectId: string, evidenceId: string, offset = 0, length = 8000) { return request<Record<string, unknown>>("evidence.preview", { project_id: projectId, evidence_id: evidenceId, offset, length }); }
}
