import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

test("a context-waiting run can continue without creating another user message", async ({ page }) => {
  const now = "2026-09-16T00:00:00+00:00";
  const conversation = {
    id: "long-chat", kind: "standard_chat", title: "Long chat", category: "research",
    resolved_category: "research", chat_group_id: null, project_id: null, sort_order: 0,
    derived_from_conversation_id: null, derived_through_message_id: null, derived_at: null,
    copied_from_conversation_id: null, copied_through_message_id: null, copied_at: null,
    provider_profile_id: null, model_id: null, created_at: now, updated_at: now, deleted_at: null,
    messages: [{ id: "user-1", conversation_id: "long-chat", role: "user", content: "continue",
      event_type: null, source_message_id: null, created_at: now, attachments: [] }],
  };
  const waiting = {
    id: "run-wait", conversation_id: conversation.id, user_message_id: "user-1",
    status: "waiting_context", provider_profile_id: "builtin-demo", model_id: "demo",
    error_code: "context_compaction_incomplete", error_message: "more context", draft: null,
    assistant_message: null,
  };
  await installTauriMock(page, {
    list_conversations: [conversation], get_conversation: conversation,
    get_conversation_model_run: { run: waiting },
    list_tool_operations: { operations: [] },
    continue_model_run_context: { run: { ...waiting, status: "queued", error_code: null, error_message: null } },
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "Long chat", exact: true }).click();
  await expect(page.getByText("More earlier context must be organized")).toBeVisible();
  await page.getByRole("button", { name: "Continue organizing and send" }).click();
  await expect(page.getByRole("button", { name: "Stop generating" })).toBeVisible();
  await expect(page.getByText("continue", { exact: true })).toHaveCount(1);
});
