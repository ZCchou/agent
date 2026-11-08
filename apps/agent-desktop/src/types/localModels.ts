export type LocalDevice = "cpu" | "cuda" | "vulkan" | "auto";
export interface LocalPathSelection { grant_id: string; path: string }
export interface LocalModelError { code: string; message: string }
export interface LocalModel {
  id: string; provider_profile_id: string; name: string; architecture: string;
  quantization: string; context_length: number; max_context_length: number;
  size_bytes: number; digest: string; template_digest?: string; adapter_digest?: string;
  device: LocalDevice; gpu_layers: number; threads: number;
  status: "imported" | "loading" | "loaded" | "error";
  is_default: boolean; source: string; license: string; error?: LocalModelError | string | null;
  files: unknown[]; created_at: string; source_type: "managed_local";
  capabilities: Record<string, unknown>; tool_mode: string;
}
export interface LocalModelConfiguration {
  device?: LocalDevice; context_length?: number; gpu_layers?: number; threads?: number;
  is_default?: boolean; source?: string; license?: string;
}
export interface LocalModelJob {
  id: string; kind?: string; state?: string; status?: string; progress: number;
  error?: LocalModelError | string | null;
}
export interface LocalModelList { models: LocalModel[]; root: string; storage_migration?: unknown }
export interface LocalRuntimeStatus {
  state: string; model_id?: string | null; backend?: string | null; session_id?: string | null;
  gpu_layers_used?: number; context_length?: number; error?: LocalModelError | string | null; fallbacks?: unknown[];
  preflight?: { physical_memory_pressure?: boolean; [key: string]: unknown };
}
export interface LocalRuntimeCapabilities { devices: unknown[]; runtimes: unknown[]; resources?: unknown; error?: unknown }
