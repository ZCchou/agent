import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

function textMessage(id, role, content, createdAt) {
  return { id, conversation_id: "work-1", role, content, event_type: null, source_message_id: null, created_at: createdAt, attachments: [] };
}

test("completed file work is collapsed in the timeline and history has a jump-to-bottom control", async ({ page }) => {
  const now = "2026-09-16T00:00:00+00:00";
  const messages = [];
  for (let index = 0; index < 18; index += 1) {
    messages.push(textMessage(`u-${index}`, "user", `Question ${index}\n`.repeat(3), now));
    messages.push(textMessage(`a-${index}`, "assistant", `Answer ${index}\n`.repeat(5), now));
  }
  messages.push({
    ...textMessage("tool-1", "system_event", JSON.stringify({
      run_id: "run-files", operation_id: "op-files", tool_name: "fs.propose_changes", status: "completed", output_bytes: 120,
    }), now),
    event_type: "tool_operation",
  });
  messages.push(textMessage("a-final", "assistant", "Files are ready.", now));
  const changeSet = {
    id: "change-1", operation_id: "op-files", run_id: "run-files", conversation_id: "work-1", project_id: "project-1",
    title: "Create training files", risk: "medium", approval_mode: "human",
    mutations: [{ kind: "create_text", path: "train.py", content: "print('ok')" }, { kind: "create_text", path: "README.md", content: "docs" }],
    status: "completed", expires_at_epoch_ms: 0, recovery_point_id: "recovery-1", created_at: now,
    undo_state: "available", undo_sequence: 1, undo_expires_at: "2026-09-17T00:00:00+00:00", undo_block_reason: null,
  };
  const work = {
    id: "work-1", kind: "project_work", title: "Implementation work", category: null,
    resolved_category: "coding", chat_group_id: null, project_id: "project-1", sort_order: 0,
    derived_from_conversation_id: null, derived_through_message_id: null, derived_at: null,
    copied_from_conversation_id: null, copied_through_message_id: null, copied_at: null,
    created_at: now, updated_at: now, deleted_at: null, provider_profile_id: null, model_id: null, messages,
  };
  await installTauriMock(page, {
    list_conversations: [{ ...work, messages }], get_conversation: { ...work, messages },
    list_agent_change_sets: [changeSet], get_conversation_model_run: { run: null },
    list_tool_operations: { operations: [] }, undo_agent_change_set: "Restored 2 paths",
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "Implementation work", exact: true }).click();

  const activity = page.locator(".run-activity");
  await expect(activity).toHaveCount(1);
  await expect(activity.locator(".change-set-card")).toHaveCount(0);
  await activity.getByRole("button", { name: /Processed 2 file operations/ }).click();
  await expect(activity.locator(".change-set-card")).toBeVisible();
  await activity.getByRole("button", { name: "Undo changes" }).click();
  await expect(page.getByRole("alertdialog")).toContainText("cannot be redone");
  await page.getByRole("button", { name: "Cancel" }).click();

  await page.locator(".chat-messages").evaluate((node) => { node.scrollTop = 0; node.dispatchEvent(new Event("scroll")); });
  const jump = page.getByRole("button", { name: "Jump to latest message" });
  await expect(jump).toBeVisible();
  await jump.click();
  await expect.poll(() => page.locator(".chat-messages").evaluate((node) => Math.round(node.scrollHeight - node.scrollTop - node.clientHeight))).toBeLessThanOrEqual(64);
});

test("an accepted user message forces the history back to the latest content", async ({ page }) => {
  const now = "2026-09-16T00:00:00+00:00";
  const messages = [];
  for (let index = 0; index < 20; index += 1) {
    messages.push(textMessage(`user-${index}`, "user", `Question ${index}\n`.repeat(3), now));
    messages.push(textMessage(`assistant-${index}`, "assistant", `Answer ${index}\n`.repeat(5), now));
  }
  const chat = {
    id: "work-1", kind: "standard_chat", title: "Long chat", category: "casual", resolved_category: "casual",
    chat_group_id: null, project_id: null, sort_order: 0, derived_from_conversation_id: null,
    derived_through_message_id: null, derived_at: null, copied_from_conversation_id: null,
    copied_through_message_id: null, copied_at: null, created_at: now, updated_at: now,
    deleted_at: null, provider_profile_id: null, model_id: null, messages,
  };
  const userMessage = textMessage("new-user", "user", "new request", now);
  const assistantMessage = textMessage("new-assistant", "assistant", "new answer", now);
  const completedRun = {
    id: "run-new", conversation_id: chat.id, user_message_id: userMessage.id, status: "completed",
    provider_profile_id: "builtin-demo", model_id: "demo", error_code: null, error_message: null,
    draft: null, assistant_message: assistantMessage,
  };
  await installTauriMock(page, {
    list_conversations: [chat], get_conversation: chat, list_agent_change_sets: [],
    get_conversation_model_run: { run: null }, connect_model_provider: null,
    start_model_run: { conversation: chat, user_message: userMessage, run: { ...completedRun, status: "generating", assistant_message: null } },
    get_model_run: { run: completedRun },
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "Long chat", exact: true }).click();
  await page.locator(".chat-messages").evaluate((node) => { node.scrollTop = 0; node.dispatchEvent(new Event("scroll")); });
  await expect(page.getByRole("button", { name: "Jump to latest message" })).toBeVisible();
  await page.getByPlaceholder("Ask agent...").fill("new request");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("new answer", { exact: true })).toBeVisible();
  await expect.poll(() => page.locator(".chat-messages").evaluate((node) => Math.round(node.scrollHeight - node.scrollTop - node.clientHeight))).toBeLessThanOrEqual(64);
});

test("a completed web request without evidence is not presented as sourced research", async ({ page }) => {
  const now = "2026-09-18T00:00:00+00:00";
  const messages = [
    textMessage("web-user", "user", "Search the latest anime", now),
    { ...textMessage("web-progress", "system_event", JSON.stringify({ version: 1, run_id: "web-run", attempt_id: "attempt-web", round_no: 1, content: "Checking sources." }), now), event_type: "assistant_progress" },
    textMessage("web-assistant", "assistant", "The request returned no verifiable sources.", now),
  ];
  const chat = {
    id: "work-1", kind: "standard_chat", title: "Evidence check", category: "research",
    resolved_category: "research", chat_group_id: null, project_id: null, sort_order: 0,
    derived_from_conversation_id: null, derived_through_message_id: null, derived_at: null,
    copied_from_conversation_id: null, copied_through_message_id: null, copied_at: null,
    created_at: now, updated_at: now, deleted_at: null, provider_profile_id: null,
    model_id: null, messages,
  };
  const operation = {
    id: "web-operation", run_id: "web-run", conversation_id: chat.id,
    user_message_id: "web-user", sequence: 1, route: "native", backend_id: "provider-search",
    provider_profile_id: "provider", model_id: "model", query_text: "latest anime",
    status: "completed", result_count: 0, evidence_status: "provider_reference",
    verified_source_count: 0, evidence_manifest_hash: null,
    provider_reference_id: "opaque-search-id", usage: { search_calls: 1 },
    error_code: null, sources: [],
  };
  await installTauriMock(page, {
    list_conversations: [chat], get_conversation: chat, list_agent_change_sets: [],
    get_conversation_model_run: { run: null }, list_tool_operations: { operations: [] },
    list_conversation_web_operations: { operations: [operation] },
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "Evidence check", exact: true }).click();

  const activity = page.locator(".run-activity");
  await expect(activity.getByRole("button", {
    name: /Web request complete · no verifiable sources returned/,
  })).toBeVisible();
  await expect(activity.locator(".run-activity__summary")).toHaveAttribute("aria-expanded", "false");
  await activity.locator(".run-activity__summary").click();
  await expect(activity).toContainText("The provider returned a search record but no verifiable sources");
  await expect(activity).not.toContainText("Obtained 1 verifiable sources");
});
