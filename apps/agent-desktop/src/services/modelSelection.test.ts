import { describe, expect, it } from "vitest";
import type { Project, ProjectModelScope } from "../types/domain";
import type {
  AppSettings, ModelCapabilities, ProviderModelSettings, ProviderProfileSettings,
} from "../types/settings";
import { resolveSelectedModel, selectableModels } from "./modelSelection";

const capabilities: ModelCapabilities = {
  text: "yes",
  vision: "unknown",
  vision_source: "unknown", vision_status: "unknown", vision_error_code: null,
  vision_verified_at: null,
  reasoning: "unknown",
  tools: "unknown",
  context_length: null,
  max_output_tokens: null,
  context_length_source: "fallback",
  input_price_per_million: null,
  output_price_per_million: null,
};

function model(id: string, overrides: Partial<ProviderModelSettings> = {}): ProviderModelSettings {
  return {
    id,
    source: "discovered",
    status: "available",
    enabled: true,
    allow_automatic_routing: false,
    tool_mode: "unknown",
    capabilities,
    last_seen_at: null,
    ...overrides,
  };
}

function provider(
  id: string,
  kind: ProviderProfileSettings["kind"],
  baseUrl: string,
  models: ProviderModelSettings[],
): ProviderProfileSettings {
  return {
    id,
    name: id,
    kind,
    preset_id: kind === "fake" ? "builtin_demo" : "custom",
    entered_base_url: baseUrl,
    resolved_api_base_url: baseUrl,
    endpoint_mode: "exact",
    enabled: true,
    credential_ref: null,
    models,
    last_discovered_at: null,
    primary_protocol: kind === "fake" ? "memory" : "openai_chat",
    native_search: {
      adapter_id: "none", protocol: "none", endpoint_base_url: null,
      source: "manual", adapter_version: "1",
    },
  };
}

const settings = {
  models: {
    providers: [
      provider("demo-provider", "fake", "builtin://demo", [model("demo")]),
      provider("deepseek-provider", "openai_compatible", "https://api.deepseek.com", [
        model("deepseek-v4-pro"),
      ]),
    ],
    default_provider_profile_id: "deepseek-provider",
    default_model_id: "deepseek-v4-pro",
    connect_timeout_secs: 15,
    first_output_timeout_secs: 60,
    chunk_timeout_secs: 30,
    total_timeout_secs: 600,
  },
} as AppSettings;

function project(overrides: Partial<Project> = {}): Project {
  return {
    id: "project-a",
    name: "Project A",
    category: "coding",
    sort_order: 0,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    model_access_policy: "local_only",
    model_scope_policy: "inherit_global",
    ...overrides,
  };
}

describe("selectableModels", () => {
  it("does not treat an online global default as selected in a local-only derived work", () => {
    const candidates = selectableModels(settings, project());

    expect(candidates.map((item) => item.value)).toEqual(["demo-provider::demo"]);
    expect(resolveSelectedModel(candidates, "deepseek-provider", "deepseek-v4-pro")).toBeNull();
    expect(resolveSelectedModel(candidates, "demo-provider", "demo")?.model.id).toBe("demo");
  });

  it("applies a restricted project model set", () => {
    const restrictedProject = project({ model_access_policy: "allow_online", model_scope_policy: "restricted" });
    const scope: ProjectModelScope = {
      project_id: restrictedProject.id,
      policy: "restricted",
      models: [{ provider_profile_id: "deepseek-provider", model_id: "deepseek-v4-pro" }],
    };

    expect(selectableModels(settings, restrictedProject, scope).map((item) => item.value))
      .toEqual(["deepseek-provider::deepseek-v4-pro"]);
  });

  it("ignores a stale model scope loaded for another project", () => {
    const staleScope: ProjectModelScope = {
      project_id: "project-b",
      policy: "restricted",
      models: [],
    };

    expect(selectableModels(settings, project(), staleScope).map((item) => item.value))
      .toEqual(["demo-provider::demo"]);
  });

  it("excludes disabled, unavailable, and non-text models", () => {
    const filteredSettings = {
      ...settings,
      models: {
        ...settings.models,
        providers: [provider("local", "ollama", "http://127.0.0.1:11434", [
          model("disabled", { enabled: false }),
          model("missing", { status: "unavailable" }),
          model("vision-only", { capabilities: { ...capabilities, text: "no" } }),
        ])],
      },
    };

    expect(selectableModels(filteredSettings as AppSettings, project())).toEqual([]);
  });
});

it("keeps cold managed models visible for loading while excluding them from execution candidates", () => {
  const id = "a".repeat(64);
  const managedSettings = { ...settings, models: { ...settings.models, providers: [provider(`managed:${id}`, "openai_compatible", "", [model(id, { status: "unavailable", allow_automatic_routing: true })])] } };
  expect(selectableModels(managedSettings, project())).toEqual([]);
  expect(selectableModels(managedSettings, project(), null, true).map(item => item.value)).toEqual([`managed:${id}::${id}`]);
  managedSettings.models.providers[0].models[0].status = "available";
  expect(selectableModels(managedSettings, project())).toHaveLength(1);
});
