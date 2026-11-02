import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

test("a derived work does not present an online global default as a usable local model", async ({ page }) => {
  const now = "2026-09-15T00:00:00+00:00";
  const project = {
    id: "project-local", name: "Local project", category: "coding", sort_order: 0,
    created_at: now, updated_at: now, deleted_at: null,
    model_access_policy: "local_only", model_scope_policy: "inherit_global",
  };
  const work = {
    id: "derived-work", kind: "project_work", title: "Derived work", category: null,
    resolved_category: "coding", chat_group_id: null, project_id: project.id, sort_order: 0,
    derived_from_conversation_id: "source-chat", derived_through_message_id: "source-message",
    derived_at: now, copied_from_conversation_id: null, copied_through_message_id: null,
    copied_at: null, provider_profile_id: null, model_id: null,
    created_at: now, updated_at: now, deleted_at: null, messages: [],
  };
  const modelCapabilities = {
    text: "yes", vision: "unknown", reasoning: "unknown", tools: "unknown",
    context_length: null, max_output_tokens: null, context_length_source: "fallback", input_price_per_million: null, output_price_per_million: null,
  };

  await installTauriMock(page, {
    list_projects: [project],
    list_conversations: [work],
    get_conversation: work,
    get_project_model_scope: { project_id: project.id, policy: "inherit_global", models: [] },
    get_app_settings: {
      schema_version: 5,
      general: { interface_language: "en" },
      sidebar: {
        standard_chats_collapsed: false, projects_collapsed: false,
        collapsed_chat_group_ids: [], collapsed_project_ids: [], width_px: 280,
      },
      models: {
        providers: [
          {
            id: "builtin-demo", name: "Built-in Demo", kind: "fake", preset_id: "builtin_demo",
            entered_base_url: "memory://fake", resolved_api_base_url: "memory://fake",
            endpoint_mode: "preset", enabled: true, credential_ref: null,
            models: [{ id: "demo", source: "discovered", status: "available", enabled: true,
              capabilities: modelCapabilities, last_seen_at: null }], last_discovered_at: null,
          },
          {
            id: "deepseek", name: "DeepSeek", kind: "openai_compatible", preset_id: "deepseek",
            entered_base_url: "https://api.deepseek.com", resolved_api_base_url: "https://api.deepseek.com",
            endpoint_mode: "preset", enabled: true, credential_ref: "credential-ref",
            models: [{ id: "deepseek-chat", source: "discovered", status: "available", enabled: true,
              capabilities: modelCapabilities, last_seen_at: null }], last_discovered_at: null,
          },
        ],
        default_provider_profile_id: "deepseek", default_model_id: "deepseek-chat",
        connect_timeout_secs: 15, first_output_timeout_secs: 60,
        chunk_timeout_secs: 30, total_timeout_secs: 600,
      },
    },
  });

  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "Derived work", exact: true }).click();

  const modelSelect = page.locator(".top-bar select").last();
  await expect(modelSelect).toHaveValue("");
  await expect(modelSelect.locator("option:checked")).toHaveText("Choose a model available to this conversation");
  await expect(modelSelect.locator("option")).toHaveText([
    "Choose a model available to this conversation",
    "Built-in Demo / demo",
  ]);

  await page.getByPlaceholder("Ask agent...").fill("continue the derived work");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("The current model is not allowed for this conversation or project.", { exact: false }))
    .toBeVisible();
});
