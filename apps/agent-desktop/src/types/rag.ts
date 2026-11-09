export type RagPathKind = "model" | "documents" | "root";
export interface RagPathSelection { grant_id: string; paths: string[] }
export type RagProfile = "qwen3-embedding-0.6b" | "qwen3-reranker-0.6b" | "bge-m3" | "bge-reranker-v2-m3";
export interface RagModel {
  id: string; name: string; role: string; backend: string; profile: string; device: string;
  status: string; digest: string; dimension: number | null; size_bytes: number; error?: string;
}
export interface RagModelList { models: RagModel[]; root: string; runtime: Record<string, unknown> }
export interface RagDocument {
  id: string; version_id: string; title: string; revision: string; status: string;
  warnings: string[]; chunk_count: number; source?: string; document_number?: string;
}
export interface RagLibrary {
  id: string; name: string; embedding_model_id: string; reranker_model_id: string | null;
  project_id?: string | null; project_ids?: string[]; active_index_id: string | null;
  index_id?: string | null; bound_index_id?: string | null;
  generation: number; documents: RagDocument[]; enabled?: boolean | number;
}
export interface RagLibraryDraft { name: string; embedding_model_id: string; reranker_model_id: string | null }
export interface RagChunk { id?: string; text: string; clause_number?: string; locator?: string | Record<string, unknown> }
export interface RagDocumentPreview { version: RagDocument; chunks: RagChunk[]; warnings: string[]; total_chunks?: number; offset?: number }
export interface RagDocumentConfirmation {
  version_id: string; title: string; revision: string; source: string; document_number: string; warnings_acknowledged: boolean;
}
export interface RagJob { id: string; kind: string; library_id: string; status: string; progress: number; error?: string }
export interface RagEvidence {
  id: string; library_id: string; version_id: string; title: string; revision: string;
  clause_number: string; locator: string | Record<string, unknown>; text: string; text_hash: string; score: number;
}
export interface RagSearchResult { evidence: RagEvidence[]; generation: number; index_ids: string[] }
export interface OcrImportOptions {
  ocr_mode: "auto" | "force" | "off";
  device: "auto" | "cpu" | "cuda";
  auto_index: boolean;
  auto_apply_project_id?: string;
  profile_id?: string;
  pages?: number[];
}
export interface OcrRegion {
  id: string; kind: string; text: string; bbox: number[]; review_status: string;
  quality_warning?: string; table?: { rows: string[][] };
}
export interface OcrPage { page: number; regions: OcrRegion[] }
export interface OcrCatalog {
  models: { id: string; name?: string }[];
  runtime: { available: boolean; error?: string };
}
