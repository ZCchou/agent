import type {
  AppSettings,
  InterfaceLanguage,
  RemoteImagePolicy,
  RoutingPreferences,
  SidebarPreferences,
  WebSearchSettings,
} from "../../types/settings";


/**
 * agent 全局设置的数据访问边界。
 *
 * React 页面只依赖这个接口，
 * 不关心设置最终由 JSON、SQLite
 * 还是其他持久化设施保存。
 */
export interface SettingsRepository {
  getAppSettings():
    Promise<AppSettings>;

  updateInterfaceLanguage(
    language:
      InterfaceLanguage,
  ): Promise<AppSettings>;
  updateSidebarPreferences(preferences: SidebarPreferences): Promise<AppSettings>;
  updateSidebarWidth(widthPx: number): Promise<AppSettings>;
  updateRemoteImagePolicy(policy: RemoteImagePolicy): Promise<AppSettings>;
  updateWebSearch(settings: Partial<WebSearchSettings>): Promise<AppSettings>;
  updateRoutingPreferences(preferences: Partial<RoutingPreferences>): Promise<AppSettings>;
  updateModelVisionConfirmation(
    providerId: string, modelId: string, confirmed: boolean | null,
  ): Promise<AppSettings>;
}
