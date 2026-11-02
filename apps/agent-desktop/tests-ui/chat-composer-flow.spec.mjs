import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

async function openChat(page, history = false, active = false) {
  const now = "2026-10-24T08:00:00Z";
  const chat = { id: "composer-chat", kind: "standard_chat", title: "Composer flow", category: "casual",
    resolved_category: "casual", project_id: null, chat_group_id: null, sort_order: 0,
    provider_profile_id: "builtin-demo", model_id: "demo", model_selection_mode: "fixed",
    created_at: now, updated_at: now, deleted_at: null, messages: [] };
  const user = { id: "sent-user", conversation_id: chat.id, role: "user", content: "First question",
    created_at: now, event_type: null, source_message_id: null, attachments: [] };
  const run = { id: "composer-run", conversation_id: chat.id, user_message_id: user.id, status: "generating",
    provider_profile_id: "builtin-demo", model_id: "demo", draft: { content: "Partial answer" },
    error_code: null, error_message: null, assistant_message: null };
  if (history) chat.messages = [
    { ...user, id: "earlier-question", content: "Earlier question" },
    { ...user, id: "earlier-answer", role: "assistant", content: "Earlier answer" }, user,
    { ...user, id: "later-answer", role: "assistant", content: "Later answer" },
  ];
  await installTauriMock(page, { list_conversations: [chat], get_conversation: chat,
    get_conversation_model_run: { run: active ? run : null }, connect_model_provider: null, get_model_run: { run },
    import_clipboard_image: { accepted: [{ id: "next-draft-image", kind: "image", name: "next.png", media_type: "image/png",
      byte_size: 4, sha256: "next-hash", storage_key: "message-attachments/next.png" }], rejected: [] },
    preview_staged_attachment: { id: "next-draft-image", kind: "image", name: "next.png", media_type: "image/png", data_url: null },
    discard_staged_attachment: null,
    cancel_model_run: { run: { ...run, status: "cancelled", error_code: "cancelled_by_user" } } });
  await page.addInitScript(({ chat, user, run }) => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    window.__composerStarts = []; window.__composerCancels = [];
    let branch = null;
    const listeners = new Map();
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "plugin:event|listen") listeners.set(args.event, args.handler);
      if (command === "update_conversation_routing") return new Promise((resolve) => {
        window.__finishComposerRouting = () => resolve({ conversation: { ...chat, model_selection_mode: "automatic" } });
      });
      if (command === "start_model_run") {
        window.__composerStarts.push(args.request);
        return new Promise((resolve, reject) => {
          window.__acceptComposer = () => {
            const edited = Boolean(args.request.edit_message_id);
            const sent = { ...user, content: args.request.content, conversation_id: edited ? "edited-chat" : chat.id };
            if (edited) branch = { ...chat, id: "edited-chat", copied_from_conversation_id: chat.id,
              messages: [...chat.messages.slice(0, 2), sent] };
            resolve({ status: "accepted", conversation: branch ?? chat, user_message: sent,
              run: { ...run, conversation_id: sent.conversation_id } });
          };
          window.__rejectComposer = () => reject(new Error("Synthetic send rejection"));
          window.__preflightComposer = () => resolve({ status: "preflight_rejected", rejection: { reason_code: "no_compatible_execution_plan" } });
        });
      }
      if (command === "get_conversation" && args.conversationId === branch?.id) return branch;
      if (command === "list_conversations" && branch) return [branch, chat];
      if (command === "get_conversation_model_run" && args.conversationId === branch?.id) return { run: { ...run, conversation_id: branch.id } };
      if (command === "get_model_run" && window.__composerFinished) return { run: { ...run, status: "completed",
        assistant_message: { id: "finished-answer", conversation_id: chat.id, role: "assistant", content: "Completed before acknowledgement", created_at: user.created_at } } };
      if (command === "get_model_run" && window.__delayComposerGet) return new Promise((resolve) => {
        window.__finishComposerGet = () => resolve({ run: { ...run, status: "waiting_context" } });
      });
      if (command === "cancel_model_run" && window.__composerFinished) {
        window.__composerCancels.push(args.runId);
        throw new Error("invalid_run_transition: Only an active run can be cancelled.");
      }
      if (command === "retry_model_run") return { run };
      if (command === "cancel_model_run" && window.__delayComposerCancel) {
        window.__composerCancels.push(args.runId);
        return new Promise((resolve) => { window.__finishComposerCancel = () => resolve({ run: { ...run, status: "cancelled", draft: { content: "Old cancelled draft" } } }); });
      }
      if (command === "cancel_model_run") window.__composerCancels.push(args.runId);
      return invoke(command, args);
    };
    window.__emitComposer = (type, data) => window[`_${listeners.get("model-run-event")}`]({
      event: "model-run-event", id: 1, payload: { run_id: run.id, event_type: type, timestamp: user.created_at, data },
    });
  }, { chat, user, run });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: chat.title, exact: true }).click();
  return page.getByRole("textbox", { name: "Ask agent..." });
}

async function sendDelayed(page, editor) {
  await editor.fill("First question");
  await page.locator(".chat-composer").getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__composerStarts.length)).toBe(1);
}

test("send immediately clears the composer, moves the question into history and switches to stop", async ({ page }) => {
  const editor = await openChat(page);
  await sendDelayed(page, editor);
  await expect(editor).toHaveValue("");
  await expect(page.locator(".chat-message--user")).toHaveText(/First question/);
  await expect(page.locator(".chat-composer").getByRole("button", { name: "Stop generating" })).toBeEnabled();
  await expect(page.locator(".chat-composer").getByRole("button", { name: "Send", exact: true })).toHaveCount(0);
  await editor.fill("Next draft");
  await editor.press("Enter");
  expect(await page.evaluate(() => window.__composerStarts.length)).toBe(1);
  await page.evaluate(() => window.__acceptComposer());
  await expect(editor).toHaveValue("Next draft");
  await expect(page.locator(".chat-message--user")).toHaveCount(1);
});

test("a sent question can be edited or discarded, and resending continues from its history point", async ({ page }) => {
  const editor = await openChat(page, true);
  await editor.fill("Keep my next draft");
  const target = page.locator(".chat-message--user").nth(1);
  await target.getByRole("button", { name: "Edit message", exact: true }).click();
  const edit = target.getByRole("textbox", { name: "Edit question" });
  await expect(edit).toHaveValue("First question");
  await edit.fill("Discarded revision");
  await target.getByRole("button", { name: "Cancel edit", exact: true }).click();
  await expect(target).toContainText("First question");
  expect(await page.evaluate(() => window.__composerStarts)).toHaveLength(0);
  await target.getByRole("button", { name: "Edit message", exact: true }).click();
  await edit.fill("Revised question");
  await page.screenshot({ path: "../../.artifacts/chat-composer-flow-2026-10-24/edit-question.png", fullPage: true });
  await target.getByRole("button", { name: "Save and regenerate", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__composerStarts.length)).toBe(1);
  expect(await page.evaluate(() => window.__composerStarts[0])).toMatchObject({
    conversation_id: "composer-chat", edit_message_id: "sent-user", content: "Revised question",
    selection: { mode: "fixed", model_id: "demo" },
  });
  await page.evaluate(() => window.__acceptComposer());
  await expect(page.locator(".chat-message--user")).toContainText(["Earlier question", "Revised question"]);
  await expect(page.locator(".chat-message--assistant")).not.toContainText(["Later answer"]);
  await expect(editor).toHaveValue("Keep my next draft");
  await expect(page.getByRole("button", { name: "View original conversation", exact: true })).toBeVisible();
  await page.screenshot({ path: "../../.artifacts/chat-composer-flow-2026-10-24/generating-next-draft.png", fullPage: true });
});

test("stop applies the cancellation response without an event, preserves output and the next draft", async ({ page }) => {
  const editor = await openChat(page);
  await sendDelayed(page, editor);
  await page.evaluate(() => window.__acceptComposer());
  await editor.fill("Next draft");
  await page.locator(".chat-composer").getByRole("button", { name: "Stop generating" }).click();
  await expect.poll(() => page.evaluate(() => window.__composerCancels)).toEqual(["composer-run"]);
  await expect(page.locator(".chat-composer").getByRole("button", { name: "Send", exact: true })).toBeEnabled();
  await expect(editor).toHaveValue("Next draft");
  await page.locator(".chat-message--draft").getByRole("button", { name: "Work process", exact: true }).click();
  await expect(page.locator(".chat-message--draft")).toContainText("Partial answer");
  await expect(page.locator(".chat-error")).toHaveCount(0);
});

test("stop during startup cancels the accepted run once its ID arrives", async ({ page }) => {
  const editor = await openChat(page);
  await sendDelayed(page, editor);
  await page.locator(".chat-composer").getByRole("button", { name: "Stop generating" }).click();
  await expect(page.locator(".chat-composer").getByRole("button", { name: "Stopping…" })).toBeDisabled();
  await editor.fill("A later question");
  await page.evaluate(() => window.__acceptComposer());
  await expect.poll(() => page.evaluate(() => window.__composerCancels)).toEqual(["composer-run"]);
  await expect(page.locator(".chat-composer").getByRole("button", { name: "Send", exact: true })).toBeEnabled();
  await expect(editor).toHaveValue("A later question");
});

for (const mode of ["error", "preflight"]) {
  test(`${mode} restores the rejected text without retaining an unsaved history bubble`, async ({ page }) => {
    const editor = await openChat(page);
    await sendDelayed(page, editor);
    await page.evaluate((mode) => mode === "error" ? window.__rejectComposer() : window.__preflightComposer(), mode);
    await expect(editor).toHaveValue("First question");
    await expect(page.locator(".chat-message--user")).toHaveCount(0);
    await expect(page.locator(".chat-composer").getByRole("button", { name: "Send", exact: true })).toBeEnabled();
  });
}

test("send rejection never overwrites a newer draft", async ({ page }) => {
  const editor = await openChat(page);
  await sendDelayed(page, editor);
  await editor.fill("Keep this newer draft");
  await page.evaluate(() => window.__rejectComposer());
  await expect(editor).toHaveValue("Keep this newer draft");
  await expect(page.locator(".chat-error")).toContainText("Synthetic send rejection");
});

test("late acceptance does not clear or inject messages into another conversation", async ({ page }) => {
  const editor = await openChat(page);
  await sendDelayed(page, editor);
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await editor.fill("A different conversation");
  await page.evaluate(() => window.__acceptComposer());
  await expect(editor).toHaveValue("A different conversation");
  await expect(page.locator(".chat-message--user")).toHaveCount(0);
  await expect(page.locator(".chat-composer").getByRole("button", { name: "Send", exact: true })).toBeEnabled();
});

test("stop before acknowledgement reconciles a run that already completed", async ({ page }) => {
  const editor = await openChat(page);
  await sendDelayed(page, editor);
  await page.locator(".chat-composer").getByRole("button", { name: "Stop generating" }).click();
  await editor.fill("Keep the next question");
  await page.evaluate(() => { window.__composerFinished = true; window.__acceptComposer(); });
  await expect(page.locator(".chat-message--assistant")).toContainText("Completed before acknowledgement");
  await expect(page.locator(".chat-composer").getByRole("button", { name: "Send", exact: true })).toBeEnabled();
  await expect(editor).toHaveValue("Keep the next question");
  await expect(page.locator(".chat-error")).toHaveCount(0);
});

test("a late cancellation response cannot replace a newer retry of the same run", async ({ page }) => {
  const editor = await openChat(page);
  await sendDelayed(page, editor);
  await page.evaluate(() => { window.__acceptComposer(); window.__delayComposerCancel = true; });
  await page.locator(".chat-composer").getByRole("button", { name: "Stop generating" }).click();
  await page.evaluate(() => window.__emitComposer("run.cancelled", { error_code: "cancelled_by_user", fallback_content: "Stopped draft" }));
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await page.evaluate(() => {
    window.__emitComposer("run.state_changed", { status: "generating" });
    window.__emitComposer("run.chunk", { delta: "New retry output" });
    window.__finishComposerCancel();
  });
  await page.locator(".chat-run-message").getByRole("button", { name: "Work process", exact: true }).click();
  await expect(page.locator(".chat-run-message__content")).toContainText("New retry output");
  await expect(page.locator(".chat-message--draft")).toHaveCount(0);
  await expect(page.locator(".chat-composer").getByRole("button", { name: "Stop generating" })).toBeEnabled();
});

test("a late routing update cannot redirect a question in another conversation", async ({ page }) => {
  const editor = await openChat(page);
  await page.locator(".conversation-routing-control > button").click();
  await page.locator(".conversation-routing-menu button").first().click();
  await expect.poll(() => page.evaluate(() => Boolean(window.__finishComposerRouting))).toBe(true);
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await editor.fill("Different conversation question");
  await page.evaluate(() => window.__finishComposerRouting());
  await page.locator(".chat-composer").getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__composerStarts.length)).toBe(1);
  expect(await page.evaluate(() => window.__composerStarts[0].conversation_id)).toBeNull();
});

test("a late waiting-context query cannot insert controls into another conversation", async ({ page }) => {
  const editor = await openChat(page);
  await sendDelayed(page, editor);
  await page.evaluate(() => window.__acceptComposer());
  await expect(page.locator(".chat-message--user")).toHaveCount(1);
  await page.evaluate(() => {
    window.__delayComposerGet = true;
    window.__emitComposer("run.waiting_context", {});
  });
  await expect.poll(() => page.evaluate(() => Boolean(window.__finishComposerGet))).toBe(true);
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await editor.fill("A separate draft");
  await page.evaluate(() => window.__finishComposerGet());
  await expect(page.locator(".chat-message--context-waiting")).toHaveCount(0);
  await expect(editor).toHaveValue("A separate draft");
});

test("editing a sent question stops its active answer before opening the editor", async ({ page }) => {
  const editor = await openChat(page, true, true);
  await editor.fill("Keep this draft");
  const target = page.locator(".chat-message--user").nth(1);
  await target.getByRole("button", { name: "Edit message", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__composerCancels)).toEqual(["composer-run"]);
  await expect(target.getByRole("textbox", { name: "Edit question" })).toHaveValue("First question");
  await expect(editor).toHaveValue("Keep this draft");
});

test("unsent images follow the next draft when an edited question creates a continuation", async ({ page }) => {
  const editor = await openChat(page, true);
  await editor.fill("Next question with an image");
  await editor.evaluate((element) => {
    const data = new DataTransfer();
    data.items.add(new File([new Uint8Array([137, 80, 78, 71])], "next.png", { type: "image/png" }));
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
  });
  await expect(page.locator(".attachment-chip")).toContainText("next.png");
  const target = page.locator(".chat-message--user").nth(1);
  await target.getByRole("button", { name: "Edit message", exact: true }).click();
  await target.getByRole("textbox", { name: "Edit question" }).fill("Revised question");
  await target.getByRole("button", { name: "Save and regenerate", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__composerStarts.length)).toBe(1);
  expect(await page.evaluate(() => window.__composerStarts[0].attachment_staging_ids)).toEqual([]);
  await page.evaluate(() => window.__acceptComposer());
  await expect(page.getByRole("button", { name: "View original conversation", exact: true })).toBeVisible();
  await expect(editor).toHaveValue("Next question with an image");
  await expect(page.locator(".attachment-chip")).toContainText("next.png");
});
