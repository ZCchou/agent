import { beforeEach, expect, it, vi } from "vitest";
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { TauriSettingsRepository } from "./TauriSettingsRepository";
import { TauriModelCatalogRepository } from "./TauriModelCatalogRepository";
import { TauriProviderRepository } from "./TauriProviderRepository";
import { TauriWebSearchRepository } from "./TauriWebSearchRepository";
import { selectableModels } from "../modelSelection";
import type { AppSettings } from "../../types/settings";
const settings = new TauriSettingsRepository();
const catalog = new TauriModelCatalogRepository();
const providers = new TauriProviderRepository();
const search = new TauriWebSearchRepository();
let publicSettings: AppSettings;
let rawSettings: AppSettings;
const id = "a".repeat(64);
beforeEach(() => {
  const model = { id, enabled: true, status: "available", capabilities: { text: "yes" } };
  rawSettings = { sidebar: { width_px: 280 }, routing: { default_preset: "balanced" }, web_search: { enabled: false }, models: { default_provider_profile_id: `managed:${id}`, default_model_id: id, providers: [{ id: "legacy-remote", enabled: true, kind: "openai_compatible", entered_base_url: "https://remote.test", models: [{ ...model, id: "remote" }] }] } } as unknown as AppSettings;
  publicSettings = structuredClone(rawSettings);
  publicSettings.models.providers[0].enabled = false;
  publicSettings.models.providers.push({ id: `managed:${id}`, name: "Managed GGUF", enabled: true, kind: "openai_compatible", entered_base_url: "", models: [model] } as AppSettings["models"]["providers"][number]);
  vi.mocked(invoke).mockReset().mockImplementation(async (command, args) => {
    if (command === "get_app_settings") return structuredClone(publicSettings);
    if (command === "update_routing_preferences") {
      publicSettings.routing = (args as { routing: AppSettings["routing"] }).routing;
      rawSettings.routing = structuredClone(publicSettings.routing);
    }
    if (command === "update_default_model") return { settings: structuredClone(rawSettings), core_sync: "pending", warning_code: "core_sync_failed" };
    return structuredClone(rawSettings);
  });
});
function expectPublicCatalog(result: AppSettings) {
  expect(result.models.providers.find(provider => provider.id === "legacy-remote")?.enabled).toBe(false);
  expect(selectableModels(result).map(candidate => candidate.value)).toEqual([`managed:${id}::${id}`]);
  expect(result.models.default_provider_profile_id).toBe(`managed:${id}`);
}
it("preserves managed default choices and offline legacy flags through get then routing patch", async () => {
  expectPublicCatalog(await settings.getAppSettings());
  const result = await settings.updateRoutingPreferences({ default_preset: "quality" });
  expect(result.routing?.default_preset).toBe("quality");
  expectPublicCatalog(result);
});
const mutations: [string, () => Promise<AppSettings>][] = [
  ["language", () => settings.updateInterfaceLanguage("zh-CN")],
  ["sidebar width", () => settings.updateSidebarWidth(300)],
  ["sidebar collapse", () => settings.updateSidebarPreferences(publicSettings.sidebar)],
  ["remote image policy", () => settings.updateRemoteImagePolicy("ask")],
  ["search settings", () => settings.updateWebSearch({ enabled: false })],
  ["vision confirmation", () => settings.updateModelVisionConfirmation("local", "model", false)],
  ["provider save", () => providers.save({ id: "local", name: "Local", kind: "ollama", preset_id: "ollama", entered_base_url: "http://127.0.0.1:11434", enabled: true })],
  ["provider refresh", () => providers.refreshModels("local")],
  ["provider enabled", () => providers.setEnabled("local", true)],
  ["provider delete", () => providers.delete("local")],
  ["model enabled", () => catalog.setEnabled("local", "model", true)],
  ["automatic routing", () => catalog.setAutomaticRouting("local", "model", true)],
  ["manual model add", () => catalog.addManual("local", "model")],
  ["manual model remove", () => catalog.removeManual("local", "model")],
  ["tool probe", () => catalog.probeTools("local", "model")],
  ["context limits", () => catalog.updateContextLimits("local", "model", 8192, null)],
  ["routing metadata", () => catalog.updateRoutingMetadata("local", "model", null, null, null, 0)],
  ["search backend save", () => search.saveBackend({ id: "search", name: "Search", kind: "searxng", baseUrl: "http://localhost", enabled: false })],
  ["search backend delete", () => search.deleteBackend("search")],
  ["native search probe", () => search.probeNative("local", "model")],
];
it.each(mutations)("refreshes the public settings view after %s", async (_name, mutate) => {
  expectPublicCatalog(await mutate());
});
it("refreshes nested default settings while preserving pending-sync metadata", async () => {
  const result = await catalog.updateDefault(`managed:${id}`, id);
  expect(result.core_sync).toBe("pending");
  expect(result.warning_code).toBe("core_sync_failed");
  expectPublicCatalog(result.settings);
});
