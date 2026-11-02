import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type { AppUpdateRepository } from "../repositories/AppUpdateRepository";
import type { RestartDraft, RestoredUpdateDraft, UpdateChannel, UpdateView } from "../../types/updater";
export class TauriAppUpdateRepository implements AppUpdateRepository {
  status(): Promise<UpdateView> { return invoke("app_update_status"); }
  check(channel: UpdateChannel): Promise<UpdateView> { return invoke("check_app_update", { channel }); }
  download(updateId: string): Promise<UpdateView> { return invoke("download_app_update", { updateId }); }
  install(updateId: string, draft: RestartDraft | null): Promise<void> { return invoke("install_app_update", { updateId, draft }); }
  loadDraft(): Promise<RestoredUpdateDraft | null> { return invoke("load_update_draft"); }
  ackDraft(updateId: string): Promise<void> { return invoke("ack_update_draft", { updateId }); }
  subscribe(listener: (view: UpdateView) => void): Promise<() => void> {
    return listen<UpdateView>("app-update-progress", (event) => listener(event.payload));
  }
}
