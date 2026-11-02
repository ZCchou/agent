import { invoke } from "@tauri-apps/api/core";
import type { AppSettings, RoutingPreferences, SidebarPreferences, WebSearchSettings } from "../../types/settings";

// Snapshot-based native settings commands share one ordered writer. The read
// belongs inside this queue, so independent controls merge against committed state.
let writes: Promise<unknown> = Promise.resolve();
export function enqueueSettingsWrite<T>(write: () => Promise<T>): Promise<T> {
  const result = writes.then(write);
  writes = result.catch(() => undefined);
  return result;
}

export function patchWebSearch(patch: Partial<WebSearchSettings>): Promise<AppSettings> {
  return enqueueSettingsWrite(async () => {
    const current = await invoke<AppSettings>("get_app_settings");
    return invoke("update_web_search_settings", { settings: { ...current.web_search, ...patch } });
  });
}

export function patchRouting(patch: Partial<RoutingPreferences>): Promise<AppSettings> {
  return enqueueSettingsWrite(async () => {
    const current = await invoke<AppSettings>("get_app_settings");
    return invoke("update_routing_preferences", { routing: { ...current.routing, ...patch } });
  });
}

export function patchSidebarCollapse(preferences: SidebarPreferences): Promise<AppSettings> {
  return enqueueSettingsWrite(async () => {
    const current = await invoke<AppSettings>("get_app_settings");
    return invoke("update_sidebar_preferences", { preferences: { ...preferences, width_px: current.sidebar.width_px } });
  });
}
