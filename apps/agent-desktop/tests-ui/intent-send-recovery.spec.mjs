import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

async function openRecoveryChat(page, initialFailure = null) {
  await installTauriMock(page, { connect_model_provider: null });
  await page.addInitScript((initialFailure) => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    window.intentStarts = [];
    window.intentTasks = [];
    let failedOnce = false;
    let accepted = null;
    window.completeIntent = () => {
      const task = window.intentTasks.at(-1);
      const now = "2026-11-13T10:00:00Z";
      const user = { id: "accepted-user", role: "user", content: task.request.content, conversation_id: "recovered-chat", attachments: [], created_at: now };
      const assistant = { id: "accepted-answer", role: "assistant", content: "Recovered answer", conversation_id: "recovered-chat", created_at: now };
      const conversation = { id: "recovered-chat", title: "Recovered conversation", kind: "standard_chat", category: "casual", resolved_category: "casual",
        project_id: null, chat_group_id: null, messages: [user, assistant], model_selection_mode: "fixed", provider_profile_id: "builtin-demo", model_id: "demo", created_at: now, updated_at: now };
      const run = { id: "recovered-run", conversation_id: conversation.id, user_message_id: user.id, status: "completed", provider_profile_id: "builtin-demo", model_id: "demo", assistant_message: assistant, draft: null };
      accepted = { status: "accepted", run, conversation, user_message: user, selection_decision: {} };
      Object.assign(task, { status: "completed", result: accepted });
    };
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "start_model_run") {
        window.intentStarts.push(structuredClone(args.request));
        let task = window.intentTasks.find((value) => value.request.idempotency_key === args.request.idempotency_key);
        if (!task) {
          task = { id: `intent-${window.intentTasks.length + 1}`, scope: args.request.intent_scope, revision: 1,
            status: "identifying", request: args.request, attempts: 1, elapsed_seconds: 1, remaining_seconds: 89,
            timeout_seconds: 90, budget_seconds: 270, budget_remaining_seconds: 269, can_retry: false, can_extend: true,
            result: null, clarification: null, error: null };
          window.intentTasks.push(task);
        }
        if (initialFailure && !failedOnce) {
          failedOnce = true;
          if (initialFailure === "uncertain") throw new Error("Admission response lost");
          Object.assign(task, { status: "failed", error: { code: "invalid_request", message: "Definitive preparation failure" } });
        }
        return { task: structuredClone(task) };
      }
      if (command === "intent_request") {
        if (args.action === "list") return { tasks: structuredClone(window.intentTasks.filter((task) => task.scope === args.params.scope).reverse()) };
        const task = window.intentTasks.find((value) => value.id === args.params.task_id);
        if (args.action === "cancel") Object.assign(task, { status: "cancelled", error: { code: "cancelled_by_user", message: "Cancelled" } });
        return { task: structuredClone(task) };
      }
      if (command === "get_conversation" && args.conversationId === accepted?.conversation.id) return structuredClone(accepted.conversation);
      if (command === "get_model_run") return { run: structuredClone(accepted.run) };
      if (command === "get_conversation_model_run" && args.conversationId === accepted?.conversation.id) return { run: structuredClone(accepted.run) };
      if (command === "list_conversations" && accepted) return [structuredClone(accepted.conversation)];
      return invoke(command, args);
    };
  }, initialFailure);
  await page.goto(`${process.env.AGENT_UI_BASE_URL ?? "http://127.0.0.1:5173"}/tests-ui/fixtures/intent-recovery.html`);
  const editor = page.getByRole("textbox", { name: "Ask agent..." });
  await expect(editor).toBeEnabled();
  return editor;
}

async function send(page, editor) {
  await editor.fill("Unchanged original question");
  await page.locator(".chat-composer").getByRole("button", { name: "Send", exact: true }).click();
}

test("Stop then unchanged Send creates a fresh task through ChatPage and the real repository", async ({ page }) => {
  const editor = await openRecoveryChat(page);
  await send(page, editor);
  await expect.poll(() => page.evaluate(() => window.intentStarts.length)).toBe(1);
  await page.getByRole("button", { name: "Stop generating", exact: true }).click();
  await expect(editor).toHaveValue("Unchanged original question");
  await page.locator(".chat-composer").getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.intentStarts.length)).toBe(2);
  const starts = await page.evaluate(() => window.intentStarts);
  expect(starts[1].idempotency_key).not.toBe(starts[0].idempotency_key);
  expect(starts[1].content).toBe(starts[0].content);
  await expect.poll(() => page.evaluate(() => window.intentTasks.length)).toBe(2);
});

test("a definitive preparation failure rotates the unchanged send identity", async ({ page }) => {
  const editor = await openRecoveryChat(page, "terminal");
  await send(page, editor);
  await expect(editor).toHaveValue("Unchanged original question");
  await page.getByRole("dialog").getByRole("button", { name: "Close", exact: true }).click();
  await page.locator(".chat-composer").getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.intentStarts.length)).toBe(2);
  const starts = await page.evaluate(() => window.intentStarts);
  expect(starts[1].idempotency_key).not.toBe(starts[0].idempotency_key);
});

test("an uncertain admission response retains the unchanged send identity", async ({ page }) => {
  const editor = await openRecoveryChat(page, "uncertain");
  await send(page, editor);
  await expect(page.locator(".chat-error")).toContainText("Admission response lost");
  await page.locator(".chat-composer").getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.intentStarts.length)).toBe(2);
  const starts = await page.evaluate(() => window.intentStarts);
  expect(starts[1].idempotency_key).toBe(starts[0].idempotency_key);
  expect(await page.evaluate(() => window.intentTasks.length)).toBe(1);
});

for (const completion of ["before", "after"]) test(`draft scope switch adopts completion ${completion} returning`, async ({ page }) => {
  const editor = await openRecoveryChat(page);
  await send(page, editor);
  await expect.poll(() => page.evaluate(() => window.intentStarts.length)).toBe(1);
  await page.getByRole("button", { name: "Other scope", exact: true }).click();
  await expect(page.getByLabel("Current scope")).toHaveText("work-1");
  if (completion === "before") {
    await page.evaluate(() => window.completeIntent());
    await page.waitForTimeout(900);
    await expect(page.getByLabel("Current scope")).toHaveText("work-1");
  }
  await page.getByRole("button", { name: "Original draft", exact: true }).click();
  if (completion === "after") {
    await expect(page.getByLabel("Current scope")).toHaveText("draft-0");
    await page.evaluate(() => window.completeIntent());
  }
  await expect(page.getByLabel("Current scope")).toHaveText("recovered-chat");
  await expect(page.locator(".chat-message--assistant")).toContainText("Recovered answer");
});
