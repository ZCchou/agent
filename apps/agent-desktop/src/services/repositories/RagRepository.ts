import type { OcrImportOptions, OcrCatalog, OcrPage, RagPathKind, RagPathSelection, RagProfile, RagModelList, RagLibrary, RagLibraryDraft, RagDocumentPreview, RagDocumentConfirmation, RagJob, RagSearchResult } from "../../types/rag";

export interface RagRepository {
  selectPaths(kind: RagPathKind, modelFile?: boolean): Promise<RagPathSelection | null>;
  revokeSelection(selection: RagPathSelection): Promise<void>;
  listModels(): Promise<RagModelList>;
  inspectModel(selection: RagPathSelection, profile: RagProfile): Promise<Record<string, unknown>>;
  importModel(selection: RagPathSelection, profile: RagProfile, device: string): Promise<unknown>;
  setModelRoot(selection: RagPathSelection): Promise<unknown>;
  setModelDevice(modelId: string, device: string): Promise<unknown>;
  removeModel(modelId: string): Promise<unknown>;
  probeModel(modelId: string): Promise<unknown>;
  listLibraries(projectId?: string): Promise<{ libraries: RagLibrary[] }>;
  createLibrary(draft: RagLibraryDraft): Promise<unknown>;
  updateLibrary(libraryId: string, draft: RagLibraryDraft): Promise<unknown>;
  removeLibrary(libraryId: string): Promise<unknown>;
  bindLibrary(libraryId: string, projectId: string, enabled: boolean): Promise<unknown>;
  importDocuments(libraryId: string, selection: RagPathSelection, options?: OcrImportOptions): Promise<unknown>;
  listOcrModels(): Promise<OcrCatalog>;
  importOcrModel(selection: RagPathSelection): Promise<unknown>;
  ocrPages(versionId: string): Promise<{ pages: OcrPage[]; summary: Record<string, unknown> }>;
  renderOcrPage(versionId: string, page: number): Promise<{ data_url: string }>;
  pasteDocument(libraryId: string, title: string, content: string): Promise<unknown>;
  previewDocument(versionId: string, offset?: number, limit?: number): Promise<RagDocumentPreview>;
  confirmDocument(confirmation: RagDocumentConfirmation): Promise<unknown>;
  withdrawDocument(versionId: string): Promise<unknown>;
  listJobs(): Promise<{ jobs: RagJob[] }>;
  cancelJob(jobId: string): Promise<unknown>;
  buildIndex(libraryId: string, versionIds?: string[]): Promise<unknown>;
  search(projectId: string, query: string, libraryIds?: string[]): Promise<RagSearchResult>;
  previewEvidence(projectId: string, evidenceId: string, offset?: number, length?: number): Promise<Record<string, unknown>>;
}
