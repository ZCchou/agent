import type { RestartDraft, RestoredUpdateDraft, UpdateChannel, UpdateView } from "../../types/updater";
export interface AppUpdateRepository {
  status(): Promise<UpdateView>;
  check(channel: UpdateChannel): Promise<UpdateView>;
  download(updateId: string): Promise<UpdateView>;
  install(updateId: string, draft: RestartDraft | null): Promise<void>;
  loadDraft(): Promise<RestoredUpdateDraft | null>;
  ackDraft(updateId: string): Promise<void>;
  subscribe(listener: (view: UpdateView) => void): Promise<() => void>;
}
