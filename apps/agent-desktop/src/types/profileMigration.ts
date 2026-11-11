export interface LegacyProfile {
  id: string;
  label: string;
  root: string;
  database_bytes: number;
}

export interface MigrationState {
  status: string;
  phase?: string;
  current_file?: string;
  current_source?: string;
  current_model?: string;
  completed_bytes?: number;
  total_bytes?: number;
  copied_files?: number;
  warnings?: Array<{ code: string; source?: string; name?: string; model?: string }>;
  error?: { code: string; message?: string };
  preview?: {
    conflicting_groups: number;
    can_prepare: boolean;
    storage: Array<{ purpose: string; path: string; required_bytes: number; available_bytes: number; sufficient: boolean }>;
  };
}

export interface MigrationDiscovery {
  profiles: LegacyProfile[];
  model_root: string | null;
  latest_job_id: string | null;
  recovery_required: boolean;
}
