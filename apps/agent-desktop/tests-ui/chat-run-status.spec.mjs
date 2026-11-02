import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

function conversation(now, messages = []) {
  return {
    id: "chat-run-status", kind: "standard_chat", title: "Run status chat", category: "casual",
    resolved_category: "casual", chat_group_id: null, project_id: null, sort_order: 0,
    derived_from_conversation_id: null, derived_through_message_id: null, derived_at: null,
    copied_from_conversation_id: null, copied_through_message_id: null, copied_at: null,
    provider_profile_id: null, model_id: null, created_at: now, updated_at: now, deleted_at: null, messages,
  };
}

test("a failure event preserves its explanation when run refresh fails", async ({ page }) => {
  const chat = conversation("2026-10-24T00:00:00Z");
  const active = run(chat.id, "generating", "Partial model output.");
  await installTauriMock(page, {
    list_conversations: [chat], get_conversation: chat,
    get_conversation_model_run: { run: active }, list_tool_operations: { operations: [] },
  });
  await page.addInitScript(() => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    const listeners = new Map();
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "plugin:event|listen") listeners.set(args.event, args.handler);
      if (command === "get_model_run") throw new Error("Synthetic persistence failure");
      return invoke(command, args);
    };
    window.__emitFailure = (type, data) => {
      const handler = listeners.get("model-run-event");
      window[`_${handler}`]({ event: "model-run-event", id: 1, payload: {
        run_id: "run-generating", timestamp: "2026-10-24T00:00:01Z", event_type: type, data,
      } });
    };
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: chat.title, exact: true }).click();
  await expect(page.getByRole("button", { name: "Stop generating" })).toBeVisible();
  await page.evaluate(() => window.__emitFailure("run.response_recovering", { error_code: "internal_error" }));
  await expect(page.locator(".chat-run-message__status")).toContainText("explaining the execution issue");
  await page.evaluate(() => window.__emitFailure("run.failed", {
    error_code: "internal_error", message: "The execution encountered an error.",
    fallback_content: "Partial model output.\n\nSystem execution explanation: the internal tool failed; completion is unverified.",
  }));
  await page.locator(".chat-message--draft").getByRole("button", { name: "Work process", exact: true }).click();
  await expect(page.locator(".chat-message--draft")).toContainText("Partial model output.");
  await expect(page.locator(".chat-message--draft")).toContainText("System execution explanation");
  await expect(page.locator(".chat-error")).toContainText("Available output and the failure reason are preserved");
  await expect(page.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop generating" })).toHaveCount(0);
});

test("a delayed failure refresh cannot replace a retry on the same run", async ({ page }) => {
  const chat = conversation("2026-10-24T00:00:00Z");
  const active = run(chat.id, "generating");
  await installTauriMock(page, {
    list_conversations: [chat], get_conversation: chat, retry_model_run: { run: active },
    get_conversation_model_run: { run: active }, list_tool_operations: { operations: [] },
  });
  await page.addInitScript(() => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    const listeners = new Map();
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "plugin:event|listen") listeners.set(args.event, args.handler);
      if (command === "get_model_run") return new Promise(resolve => { window.__resolveOldFailure = resolve; });
      return invoke(command, args);
    };
    window.__emitRetryEvent = (type, data) => {
      window[`_${listeners.get("model-run-event")}`]({ event: "model-run-event", id: 1, payload: {
        run_id: "run-generating", timestamp: "2026-10-24T00:00:01Z", event_type: type, data,
      } });
    };
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: chat.title, exact: true }).click();
  await expect(page.getByRole("button", { name: "Stop generating" })).toBeVisible();
  await page.evaluate(() => window.__emitRetryEvent("run.failed", {
    error_code: "internal_error", fallback_content: "Old failed response.",
  }));
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await page.evaluate(() => {
    window.__emitRetryEvent("run.state_changed", { status: "generating" });
    window.__emitRetryEvent("run.chunk", { delta: "New retry output survives." });
    window.__resolveOldFailure({ run: { id: "run-generating", status: "failed", draft: null } });
  });
  await page.locator(".chat-run-message").getByRole("button", { name: "Work process", exact: true }).click();
  await expect(page.locator(".chat-run-message__content")).toContainText("New retry output survives.");
  await expect(page.locator(".chat-message--draft")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Stop generating" })).toBeVisible();
});

function run(conversationId, status, draft = null) {
  return {
    id: `run-${status}`, conversation_id: conversationId, user_message_id: "user-1", status,
    provider_profile_id: "builtin-demo", model_id: "demo", error_code: null, error_message: null,
    draft: draft ? { content: draft, updated_at: "2026-09-16T00:00:00+00:00" } : null,
    assistant_message: null,
  };
}

test("an empty generating run uses the compact thinking status instead of markdown", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const now = "2026-09-16T00:00:00+00:00";
  const chat = conversation(now);
  await installTauriMock(page, {
    list_conversations: [chat], get_conversation: chat,
    get_conversation_model_run: { run: run(chat.id, "generating") }, list_tool_operations: { operations: [] },
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "Run status chat", exact: true }).click();
  await expect(page.locator(".chat-run-message__status")).toContainText("Thinking");
  await expect(page.locator(".chat-run-message__content")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Stop generating" })).toBeVisible();
  expect(await page.locator(".chat-run-message__dots i").first().evaluate((element) => getComputedStyle(element).animationName)).toBe("none");
});

test("streamed markdown is muted while a completed reply uses the normal text color", async ({ page }) => {
  const now = "2026-09-16T00:00:00+00:00";
  const chat = conversation(now, [{
    id: "assistant-complete", conversation_id: "chat-run-status", role: "assistant", content: "Complete **answer**",
    event_type: null, source_message_id: null, created_at: now, attachments: [],
  }]);
  await installTauriMock(page, {
    list_conversations: [chat], get_conversation: chat,
    get_conversation_model_run: { run: run(chat.id, "generating", "Partial **answer**") }, list_tool_operations: { operations: [] },
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "Run status chat", exact: true }).click();
  await expect(page.locator(".chat-run-message__status")).toContainText("Generating");
  await page.locator(".chat-run-message").getByRole("button", { name: "Work process", exact: true }).click();
  await expect(page.locator(".chat-run-message__content strong")).toHaveText("answer");
  const mutedColor = await page.locator(".chat-run-message__content .markdown-body").evaluate((element) => getComputedStyle(element).color);
  const normalColor = await page.locator(".chat-message--assistant:not(.chat-run-message) .markdown-body").evaluate((element) => getComputedStyle(element).color);
  expect(mutedColor).not.toBe(normalColor);
});

for (const limit of [
  { name: "legacy calls", code: "tool_limit_exceeded", message: "The run exceeded its tool round limit.", visible: "tool-call limit" },
  { name: "new calls", code: "tool_call_limit_exceeded", message: "This attempt reached its 256-call tool limit.", visible: "tool-call limit" },
  { name: "read quota", code: "tool_read_limit_exceeded", message: "Read allowance exhausted.", visible: "read/search limit" },
]) {
  test(`a failed draft shows the specific ${limit.name} cause`, async ({ page }) => {
    const chat = conversation("2026-10-24T00:00:00Z");
    await installTauriMock(page, {
      list_conversations: [chat], get_conversation: chat,
      get_conversation_model_run: { run: { ...run(chat.id, "failed", "Partial work."), error_code: limit.code, error_message: limit.message } },
      list_tool_operations: { operations: [] },
    });
    await page.goto("http://127.0.0.1:5173");
    await page.getByRole("button", { name: "Run status chat", exact: true }).click();
    await expect(page.locator(".chat-message--draft > strong")).toContainText(limit.visible);
    await expect(page.locator(".chat-message--draft")).not.toContainText("safe file-operation or data limit");
    await expect(page.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
  });
}
