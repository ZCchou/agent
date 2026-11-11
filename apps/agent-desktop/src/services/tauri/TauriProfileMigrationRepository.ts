import { invoke } from '@tauri-apps/api/core';
import type { MigrationDiscovery, MigrationState } from '../../types/profileMigration';

function request<T>(action: string, jobId?: string, sourceIds?: string[]) {
  return invoke<T>('profile_migration_request', { action, jobId: jobId ?? null, sourceIds: sourceIds ?? null });
}

export class TauriProfileMigrationRepository {
  discover() { return request<MigrationDiscovery>('discover'); }
  chooseStorage() { return request<{ model_root: string } | null>('choose_storage'); }
  prepare(sourceIds: string[]) { return request<{ job_id: string }>('prepare', undefined, sourceIds); }
  confirmPrepare(jobId: string) { return request('confirm_prepare', jobId); }
  status(jobId: string) { return request<{ job_id: string; state: MigrationState; recovery_required: boolean; activated: boolean }>('status', jobId); }
  cancel(jobId: string) { return request('cancel', jobId); }
  activate(jobId: string) { return request('activate', jobId); }
  rollback(jobId: string) { return request('rollback', jobId); }
  finalize(jobId: string) { return request('finalize', jobId); }
  restart() { return request<void>('restart'); }
}
