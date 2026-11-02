import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

test("model services use aligned cards and the add-provider drawer follows the page layout", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 850 });
  await installTauriMock(page, {
    get_app_settings: {
      schema_version: 5,
      general: { interface_language: "en" },
      sidebar: {
        standard_chats_collapsed: false, projects_collapsed: false,
        collapsed_chat_group_ids: [], collapsed_project_ids: [], width_px: 280,
      },
      models: {
        providers: [
          { id: "builtin-demo", name: "Built-in Demo", kind: "fake", preset_id: "builtin_demo", entered_base_url: "memory://fake", resolved_api_base_url: "memory://fake", endpoint_mode: "preset", enabled: true, credential_ref: null, models: [{ id: "demo", source: "discovered", status: "available", enabled: true, tool_mode: "native", capabilities: { text: "yes", vision: "unknown", reasoning: "unknown", tools: "unknown", context_length: null, max_output_tokens: null, context_length_source: "fallback", input_price_per_million: null, output_price_per_million: null }, last_seen_at: null }], last_discovered_at: null },
          { id: "local-ollama", name: "Local Ollama", kind: "ollama", preset_id: "ollama", entered_base_url: "http://127.0.0.1:11434", resolved_api_base_url: "http://127.0.0.1:11434", endpoint_mode: "preset", enabled: true, credential_ref: null, models: [
            { id: "qwen", source: "discovered", status: "available", enabled: true, tool_mode: "unknown", capabilities: { text: "unknown", vision: "unknown", reasoning: "unknown", tools: "unknown", context_length: null, max_output_tokens: null, context_length_source: "fallback", input_price_per_million: null, output_price_per_million: null }, last_seen_at: null },
            { id: "old-model", source: "discovered", status: "unavailable", enabled: false, tool_mode: "text_only", capabilities: { text: "unknown", vision: "unknown", reasoning: "unknown", tools: "unknown", context_length: null, max_output_tokens: null, context_length_source: "fallback", input_price_per_million: null, output_price_per_million: null }, last_seen_at: null },
            { id: "private-model", source: "manual", status: "unverified", enabled: false, tool_mode: "structured", capabilities: { text: "unknown", vision: "unknown", reasoning: "unknown", tools: "unknown", context_length: null, max_output_tokens: null, context_length_source: "fallback", input_price_per_million: null, output_price_per_million: null }, last_seen_at: null },
          ], last_discovered_at: null },
        ],
        default_provider_profile_id: "builtin-demo", default_model_id: "demo",
        connect_timeout_secs: 15, first_output_timeout_secs: 60, chunk_timeout_secs: 30, total_timeout_secs: 600,
      },
    },
    count_conversation_model_references: 1,
    count_provider_conversation_references: 2,
  });

  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "System features" }).click();
  await page.getByRole("button", { name: "Models" }).click();

  const cards = page.locator(".model-provider-card");
  await expect(cards).toHaveCount(2);
  await expect(page.locator(".models-default-card")).toHaveCount(1);
  await expect(page.locator(".models-default-card select")).toHaveCount(2);
  await expect(cards.locator("select")).toHaveCount(0);
  const first = await cards.nth(0).boundingBox();
  const second = await cards.nth(1).boundingBox();
  expect(first).not.toBeNull();
  expect(second).not.toBeNull();
  expect(Math.abs(first.y - second.y)).toBeLessThan(2);
  expect(Math.abs(first.width - second.width)).toBeLessThan(2);

  await cards.nth(1).getByRole("button", { name: "Manage models" }).click();
  const catalog = page.getByRole("dialog", { name: "Local Ollama" });
  const catalogBox = await catalog.boundingBox();
  expect(catalogBox).not.toBeNull();
  expect(catalogBox.width).toBeGreaterThanOrEqual(680);
  expect(catalogBox.width).toBeLessThanOrEqual(721);
  await expect(catalog.locator(".model-list article")).toHaveCount(3);
  await expect(catalog.getByText("No longer reported", { exact: false })).toBeVisible();
  await expect(catalog.getByText("Manually added", { exact: false })).toBeVisible();
  await catalog.getByRole("button", { name: "Advanced parameters" }).first().click();
  await expect(catalog.getByLabel("Context window").first()).toBeVisible();
  await expect(catalog.getByLabel("Maximum output tokens").first()).toBeVisible();
  expect(await catalog.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.setViewportSize({ width: 900, height: 500 });
  const modelList = catalog.getByTestId("model-catalog-scroll");
  const overflow = await modelList.evaluate((element) => ({
    clientHeight: element.clientHeight, scrollHeight: element.scrollHeight,
  }));
  expect(overflow.scrollHeight).toBeGreaterThan(overflow.clientHeight);
  await modelList.evaluate((element) => { element.scrollTop = 100; });
  expect(await modelList.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
  await expect(catalog.getByPlaceholder("Enter the exact model ID")).toBeVisible();
  await catalog.getByRole("checkbox", { name: "Allowed only" }).check();
  await expect(catalog.locator(".model-list article")).toHaveCount(1);
  await catalog.getByRole("checkbox", { name: "Allow use" }).click();
  const modelConfirmation = page.getByRole("alertdialog", { name: /Disable qwen/ });
  await expect(modelConfirmation).toContainText("1 conversation");
  await page.keyboard.press("Escape");
  await expect(modelConfirmation).toBeHidden();
  await expect(catalog).toBeVisible();
  await catalog.getByRole("button", { name: "Close" }).click();
  await expect(cards.nth(1).getByRole("button", { name: "Manage models" })).toBeFocused();
  await cards.nth(1).getByRole("button", { name: "Disable service" }).click();
  const providerConfirmation = page.getByRole("alertdialog", { name: /Disable Local Ollama/ });
  await expect(providerConfirmation).toContainText("2 conversation");
  await providerConfirmation.getByRole("button", { name: "Cancel" }).click();

  await page.getByRole("button", { name: "Add provider" }).click();
  const dialog = page.getByRole("dialog", { name: "Add provider" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: /Ollama/ }).click();
  await expect(dialog.getByLabel("Name")).toBeFocused();
  await expect(dialog.getByRole("button", { name: "Test and continue" })).toBeEnabled();
});

test("the model catalog becomes full-width without horizontal overflow on a narrow window", async ({ page }) => {
  await page.setViewportSize({ width: 560, height: 620 });
  await installTauriMock(page, {
    get_app_settings: {
      schema_version: 7,
      general: { interface_language: "en" },
      content: { remote_image_policy: "ask" },
      sidebar: { standard_chats_collapsed: false, projects_collapsed: false, collapsed_chat_group_ids: [], collapsed_project_ids: [], width_px: 240 },
      models: {
        providers: [{ id: "local-ollama", name: "Local Ollama", kind: "ollama", preset_id: "ollama", entered_base_url: "http://127.0.0.1:11434", resolved_api_base_url: "http://127.0.0.1:11434", endpoint_mode: "preset", enabled: true, credential_ref: null, models: [{
          id: "a-very-long-model-name-that-must-not-push-the-drawer-outside-the-window", source: "discovered", status: "available", enabled: true, tool_mode: "unknown",
          capabilities: { text: "yes", vision: "unknown", reasoning: "unknown", tools: "unknown", context_length: null, max_output_tokens: null, context_length_source: "fallback", input_price_per_million: null, output_price_per_million: null }, last_seen_at: null,
        }], last_discovered_at: null }],
        default_provider_profile_id: "local-ollama", default_model_id: "a-very-long-model-name-that-must-not-push-the-drawer-outside-the-window",
        connect_timeout_secs: 15, first_output_timeout_secs: 60, chunk_timeout_secs: 30, total_timeout_secs: 600,
      },
    },
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "System features" }).click();
  await page.getByRole("button", { name: "Models" }).click();
  await page.getByRole("button", { name: "Manage models" }).click();
  const catalog = page.getByRole("dialog", { name: "Local Ollama" });
  const box = await catalog.boundingBox();
  expect(box).not.toBeNull();
  expect(box.width).toBeLessThanOrEqual(560);
  expect(await catalog.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
});

test("an official DeepSeek preset exposes declared native search without a prerequisite probe", async ({ page }) => {
  await installTauriMock(page, {
    get_app_settings: {
      schema_version: 9,
      general: { interface_language: "en" },
      content: { remote_image_policy: "ask" },
      sidebar: { standard_chats_collapsed: false, projects_collapsed: false, collapsed_chat_group_ids: [], collapsed_project_ids: [], width_px: 280 },
      web_search: { enabled: false, privacy_acknowledged: false, default_backend_id: null, safe_search: true, default_language: "auto", default_region: "auto", backends: [] },
      models: {
        providers: [{
          id: "deepseek", name: "DeepSeek", kind: "openai_compatible", preset_id: "deepseek",
          entered_base_url: "https://api.deepseek.com", resolved_api_base_url: "https://api.deepseek.com",
          endpoint_mode: "preset", enabled: true, credential_ref: "windows-credential-manager",
          primary_protocol: "openai_chat",
          native_search: { adapter_id: "anthropic_messages_web_search", protocol: "anthropic_messages", endpoint_base_url: "https://api.deepseek.com/anthropic", source: "official_preset", adapter_version: "1" },
          models: [{ id: "deepseek-chat", source: "discovered", status: "available", enabled: true, tool_mode: "native", native_web_search_status: "declared", capabilities: { text: "yes", vision: "unknown", reasoning: "unknown", tools: "yes", context_length: null, max_output_tokens: null, context_length_source: "fallback", input_price_per_million: null, output_price_per_million: null }, last_seen_at: null }],
          last_discovered_at: null,
        }],
        default_provider_profile_id: "deepseek", default_model_id: "deepseek-chat",
        connect_timeout_secs: 15, first_output_timeout_secs: 60, chunk_timeout_secs: 30, total_timeout_secs: 600,
      },
    },
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "System features" }).click();
  await page.getByRole("button", { name: "Models" }).click();
  await page.getByRole("button", { name: "Manage models" }).click();
  const catalog = page.getByRole("dialog", { name: "DeepSeek" });
  await expect(catalog.getByText("Native search available", { exact: true })).toBeVisible();
  await expect(catalog.getByRole("button", { name: "Diagnose native search" })).toBeVisible();
  await expect(catalog.getByRole("button", { name: "Acknowledge and allow" })).toBeVisible();
});
