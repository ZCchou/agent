import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

test("context inspector toggles, closes with Escape, and survives page navigation", async ({ page }) => {
  const now = "2026-09-16T00:00:00+00:00";
  const conversation = {
    id: "context-chat", kind: "standard_chat", title: "Context test", category: "research",
    resolved_category: "research", chat_group_id: null, project_id: null, sort_order: 0,
    derived_from_conversation_id: null, derived_through_message_id: null, derived_at: null,
    copied_from_conversation_id: null, copied_through_message_id: null, copied_at: null,
    provider_profile_id: null, model_id: null, created_at: now, updated_at: now, deleted_at: null,
    messages: [{ id: "m1", conversation_id: "context-chat", role: "user", content: "hello", event_type: null, source_message_id: null, created_at: now, attachments: [] }],
  };
  await installTauriMock(page, {
    list_conversations: [conversation], get_conversation: conversation,
    get_conversation_model_run: { run: {
      id: "run-1", conversation_id: conversation.id, user_message_id: "m1", status: "completed",
      provider_profile_id: "builtin-demo", model_id: "demo", error_code: null, error_message: null,
      draft: null, assistant_message: null,
    } },
    get_run_context: { snapshot: {
      id: "snapshot-1", run_id: "run-1", round_no: 0, policy_version: "context-default-v1",
      context_generation: 1, context_window: 10000, input_target: 8000, output_reserve: 2000,
      token_method: "conservative-char-v2", estimated_tokens: 42,
      stable_prefix_hash: "stable", dynamic_suffix_hash: "dynamic", retrieval_confidence: "none",
      compression_applied: 0, omitted_items: 0,
      items: [{ id: "item-1", source_type: "message", source_id: "m1", section: "recent_history",
        representation: "original", trust: "user_authored", estimated_tokens: 2,
        included: 1, inclusion_reason: "recent_turn", exclusion_reason: null, metadata: {} }],
    } },
    get_context_source: { source: {
      item_id: "item-1", source_type: "message", source: { id: "m1", content: "hello" },
    } },
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "Context test", exact: true }).click();
  const details = page.getByRole("button", { name: "Context details" });
  await expect(details).toBeVisible();
  await details.click();
  await expect(page.getByRole("dialog", { name: "Context details" })).toBeVisible();
  await expect(page.getByText("Input estimate 42 tokens")).toBeVisible();
  await expect(page.getByText("Capacity unconfirmed")).toBeVisible();
  await page.getByRole("button", { name: "View source" }).click();
  await expect(page.getByRole("dialog", { name: "Context details" }).getByText("hello", { exact: true })).toBeVisible();
  await details.click();
  await expect(page.getByRole("dialog", { name: "Context details" })).toHaveCount(0);
  await details.click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Context details" })).toHaveCount(0);
  await details.click();
  await page.getByRole("button", { name: "System features" }).click();
  await page.getByRole("button", { name: "Models" }).click();
  await page.getByRole("button", { name: "Context test", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Context details" })).toBeVisible();
});
