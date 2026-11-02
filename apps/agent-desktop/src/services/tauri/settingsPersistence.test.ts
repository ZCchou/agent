import { beforeEach, expect, it, vi } from "vitest";
import type { AppSettings, RoutingPreferences, WebSearchSettings } from "../../types/settings";
const native = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: native.invoke }));
import { TauriSettingsRepository } from "./TauriSettingsRepository";
import { TauriWebSearchRepository } from "./TauriWebSearchRepository";

let state: AppSettings;
beforeEach(() => {
  state = { sidebar: { width_px: 280, standard_chats_collapsed: false, projects_collapsed: false,
    collapsed_chat_group_ids: [], collapsed_project_ids: [] },
    routing: { default_preset: "balanced", max_model_calls_per_message: 3, max_specialist_steps: 1,
      max_extra_online_cost_usd: "0.20", allow_unknown_price_automatic: false },
    web_search: { enabled: false, privacy_acknowledged: false, default_backend_id: "existing",
      safe_search: true, default_language: "zh-CN", default_region: "TW", backends: [{ id: "existing", name: "Existing", enabled: true }] },
  } as unknown as AppSettings;
  native.invoke.mockReset();
  native.invoke.mockImplementation(async (command: string, args: Record<string, unknown>) => {
    if (command === "get_app_settings") return structuredClone(state);
    if (command === "update_sidebar_width") state.sidebar.width_px = args.widthPx as number;
    if (command === "update_sidebar_preferences") state.sidebar = structuredClone(args.preferences) as AppSettings["sidebar"];
    if (command === "update_routing_preferences") state.routing = structuredClone(args.routing) as RoutingPreferences;
    if (command === "update_web_search_settings") state.web_search = structuredClone(args.settings) as WebSearchSettings;
    return structuredClone(state);
  });
});

it("collapse writes preserve a width saved by the independent resize control", async () => {
  const repo = new TauriSettingsRepository(); const old = structuredClone(state.sidebar);
  await repo.updateSidebarWidth(360);
  await repo.updateSidebarPreferences({ ...old, projects_collapsed: true });
  expect(state.sidebar.width_px).toBe(360);
  expect(state.sidebar.projects_collapsed).toBe(true);
});

it("rapid independent routing edits persist both fields and remaining budgets", async () => {
  const repo = new TauriSettingsRepository();
  await Promise.all([
    repo.updateRoutingPreferences({ max_model_calls_per_message: 4 } as RoutingPreferences),
    repo.updateRoutingPreferences({ default_preset: "quality" } as RoutingPreferences),
  ]);
  expect(state.routing).toMatchObject({ max_model_calls_per_message: 4, default_preset: "quality", max_extra_online_cost_usd: "0.20" });
});

it("enabling search retains configured backends, default and preferences", async () => {
  await new TauriSettingsRepository().updateWebSearch({ enabled: true, privacy_acknowledged: true } as WebSearchSettings);
  expect(state.web_search).toMatchObject({ enabled: true, privacy_acknowledged: true, default_backend_id: "existing", default_region: "TW" });
  expect(state.web_search.backends[0].id).toBe("existing");
});

it("backend registration completes before a queued search preference merge", async () => {
  const original = native.invoke.getMockImplementation()!;
  let release!: () => void; const gate = new Promise<void>((resolve) => { release = resolve; });
  let entered!: () => void; const started = new Promise<void>((resolve) => { entered = resolve; });
  const order: string[] = [];
  native.invoke.mockImplementation(async (command: string, args: Record<string, unknown>) => {
    if (command === "save_search_backend") { order.push("backend-start"); entered(); await gate; order.push("backend-done"); }
    if (command === "update_web_search_settings") order.push("preferences");
    return original(command, args);
  });
  const save = new TauriWebSearchRepository().saveBackend({ id: "new", name: "New", kind: "tavily", baseUrl: "https://example.invalid", enabled: true });
  await started;
  const flags = new TauriSettingsRepository().updateWebSearch({ enabled: true } as WebSearchSettings);
  release(); await Promise.all([save, flags]);
  expect(order).toEqual(["backend-start", "backend-done", "preferences"]);
});

it("a failed save does not poison subsequent settings writes", async () => {
  const original = native.invoke.getMockImplementation()!; let first = true;
  native.invoke.mockImplementation(async (command: string, args: Record<string, unknown>) => {
    if (command === "update_routing_preferences" && first) { first = false; throw new Error("synthetic disk failure"); }
    return original(command, args);
  });
  const repo = new TauriSettingsRepository();
  await expect(repo.updateRoutingPreferences({ default_preset: "quality" } as RoutingPreferences)).rejects.toThrow("synthetic");
  await repo.updateRoutingPreferences({ max_model_calls_per_message: 5 } as RoutingPreferences);
  expect(state.routing).toMatchObject({ default_preset: "balanced", max_model_calls_per_message: 5 });
});
