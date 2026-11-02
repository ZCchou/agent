import {
  invoke,
} from "@tauri-apps/api/core";

import type {
  SettingsRepository,
} from "../repositories/SettingsRepository";

import type {
  AppSettings,
  InterfaceLanguage,
  RemoteImagePolicy,
  RoutingPreferences,
  SidebarPreferences,
  WebSearchSettings,
} from "../../types/settings";
import { enqueueSettingsWrite, patchRouting, patchSidebarCollapse, patchWebSearch } from "./settingsPersistence";


/**
 * SettingsRepository 的正式桌面实现。
 *
 * React 不直接访问 settings.json，
 * 所有持久化修改必须经过 Rust SettingsManager。
 */
export class TauriSettingsRepository
  implements SettingsRepository
{
  async getAppSettings():
    Promise<AppSettings>
  {
    return invoke<AppSettings>(
      "get_app_settings",
    );
  }


  async updateInterfaceLanguage(
    language:
      InterfaceLanguage,
  ): Promise<AppSettings> {
    return invoke<AppSettings>(
      "update_interface_language",
      {
        language,
      },
    );
  }

  async updateSidebarPreferences(preferences: SidebarPreferences): Promise<AppSettings> {
    return patchSidebarCollapse(preferences);
  }

  async updateSidebarWidth(widthPx: number): Promise<AppSettings> {
    return enqueueSettingsWrite(() => invoke<AppSettings>("update_sidebar_width", { widthPx }));
  }

  async updateRemoteImagePolicy(policy: RemoteImagePolicy): Promise<AppSettings> {
    return invoke<AppSettings>("update_remote_image_policy", { policy });
  }
  async updateWebSearch(settings: Partial<WebSearchSettings>): Promise<AppSettings> {
    return patchWebSearch(settings);
  }
  async updateRoutingPreferences(preferences: Partial<RoutingPreferences>): Promise<AppSettings> {
    return patchRouting(preferences);
  }

  async updateModelVisionConfirmation(
    providerId: string, modelId: string, confirmed: boolean | null,
  ): Promise<AppSettings> {
    return invoke<AppSettings>("update_model_vision_confirmation", {
      providerId, modelId, confirmed,
    });
  }

}
