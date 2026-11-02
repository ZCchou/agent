import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

async function openHistory(page, attachments, overrides = {}, delayStart = false) {
  const now = "2026-10-13T00:00:00+00:00";
  const conversation = {
    id: "reference-chat", kind: "standard_chat", title: "Attachment references", category: "research",
    resolved_category: "research", chat_group_id: null, project_id: null, sort_order: 0,
    provider_profile_id: "builtin-demo", model_id: "demo", model_selection_mode: "fixed",
    created_at: now, updated_at: now, deleted_at: null,
    messages: attachments.map((attachment, index) => ({
      id: `message-${index}`, conversation_id: "reference-chat", role: "user",
      content: `Source message ${index + 1}`, event_type: null, source_message_id: null,
      created_at: now, attachments: [attachment],
    })),
  };
  await installTauriMock(page, {
    list_conversations: [conversation], get_conversation: conversation,
    get_conversation_model_run: { run: null }, connect_model_provider: null,
    start_model_run: { status: "preflight_rejected", rejection: { reason_code: "no_compatible_execution_plan" } },
    ...(typeof overrides === "function" ? overrides(conversation) : overrides),
  });
  await page.addInitScript((delayStart) => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    window.__referenceRequests = [];
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "start_model_run") {
        window.__referenceRequests.push(args.request);
        if (delayStart) return new Promise((resolve) => { window.__resolveStart = () => resolve(invoke(command, args)); });
      }
      return invoke(command, args);
    };
  }, delayStart);
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: conversation.title, exact: true }).click();
  return page.getByRole("textbox", { name: "Ask agent..." });
}

function file(id, name, kind = "text") {
  return { id, name, kind, media_type: kind === "text" ? "text/plain" : "image/png",
    byte_size: 100, sha256: `hash-${id}`, width: null, height: null };
}

test("a uniquely named historical text appears as a removable reference before send", async ({ page }) => {
  const editor = await openHistory(page, [file("notes", "notes.txt")]);
  await editor.fill("Summarize notes.txt");
  const references = page.getByRole("region", { name: "Referenced attachments" });
  await expect(references.getByText("notes.txt", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__referenceRequests.length)).toBe(1);
  expect(await page.evaluate(() => window.__referenceRequests[0].attachment_references))
    .toEqual([{ attachment_id: "notes" }]);
  await references.getByRole("button", { name: "Remove reference notes.txt" }).click();
  await expect(references).toHaveCount(0);
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__referenceRequests.length)).toBe(2);
  expect(await page.evaluate(() => window.__referenceRequests[1].attachment_references ?? [])).toEqual([]);
  const keys = await page.evaluate(() => window.__referenceRequests.map((request) => request.idempotency_key));
  expect(keys[0]).not.toBe(keys[1]);
});

test("accepted send freezes historical and preview reference actions until its captured selection is cleared", async ({ page }) => {
  const editor = await openHistory(page, [file("first", "first.txt"), file("second", "second.txt")], (conversation) => {
    const userMessage = { id: "accepted-user", conversation_id: conversation.id, role: "user", content: "Compare",
      event_type: null, source_message_id: null, created_at: conversation.created_at, attachments: [] };
    const assistantMessage = { id: "accepted-answer", conversation_id: conversation.id, role: "assistant", content: "Compared",
      event_type: null, source_message_id: null, created_at: conversation.created_at, attachments: [] };
    const run = { id: "accepted-run", conversation_id: conversation.id, user_message_id: userMessage.id,
      status: "completed", provider_profile_id: "builtin-demo", model_id: "demo", error_code: null,
      error_message: null, draft: null, assistant_message: assistantMessage };
    return { preview_message_attachment: { id: "second", name: "second.txt", kind: "text",
      media_type: "text/plain", content: "Select this text", data_url: null },
    start_model_run: { conversation, user_message: userMessage, run: { ...run, status: "generating", assistant_message: null } },
    get_model_run: { run } };
  }, true);
  await page.getByRole("button", { name: "Reference first.txt", exact: true }).click();
  await page.getByRole("button", { name: "second.txt", exact: true }).click();
  const inspector = page.getByRole("dialog", { name: "second.txt", exact: true });
  await expect(inspector.locator("pre")).toContainText("Select this text");
  await editor.fill("Compare");
  await editor.press("Enter");
  await expect.poll(() => page.evaluate(() => window.__referenceRequests.length)).toBe(1);
  const selectPreview = () => inspector.locator("pre").evaluate((element) => {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(element);
    selection.removeAllRanges();
    selection.addRange(range);
    document.dispatchEvent(new Event("selectionchange"));
  });
  await selectPreview();
  await expect(page.getByRole("button", { name: "Reference second.txt", exact: true })).toBeDisabled();
  await expect(inspector.getByRole("button", { name: "Reference selected text", exact: true })).toBeDisabled();
  expect(await page.evaluate(() => window.__referenceRequests[0].attachment_references))
    .toEqual([{ attachment_id: "first" }]);
  await page.evaluate(() => window.__resolveStart());
  await expect(page.getByRole("region", { name: "Referenced attachments" })).toHaveCount(0);
  await selectPreview();
  await expect(inspector.getByRole("button", { name: "Reference selected text", exact: true })).toBeEnabled();
});

test("same-name references require a visible choice and Enter cannot bypass it", async ({ page }) => {
  const editor = await openHistory(page, [file("old", "notes.txt"), file("new", "notes.txt")]);
  await editor.fill("Summarize notes.txt");
  await expect(page.getByText("Choose the attachment to reference before sending.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeDisabled();
  await editor.press("Enter");
  expect(await page.evaluate(() => window.__referenceRequests)).toEqual([]);
  const candidates = page.getByRole("region", { name: "Choose attachment references" });
  await expect(candidates.getByText("Source message 1")).toBeVisible();
  await expect(candidates.getByText("Source message 2")).toBeVisible();
  await candidates.getByRole("button", { name: "Reference notes.txt" }).first().click();
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__referenceRequests.length)).toBe(1);
  expect(await page.evaluate(() => window.__referenceRequests[0].attachment_references))
    .toEqual([{ attachment_id: "old" }]);
});

test("an explicitly selected image can be sent without text and selection never exposes a path", async ({ page }) => {
  await openHistory(page, [file("image", "chart.png", "image")]);
  await page.getByRole("button", { name: "Reference chart.png", exact: true }).click();
  await expect(page.getByRole("region", { name: "Referenced attachments" }).getByText("chart.png", { exact: true }))
    .toBeVisible();
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__referenceRequests.length)).toBe(1);
  expect(await page.evaluate(() => window.__referenceRequests[0].attachment_references))
    .toEqual([{ attachment_id: "image" }]);
});

test("selected preview text uses Unicode character offsets in the visible reference", async ({ page }) => {
  await openHistory(page, [file("notes", "notes.txt")], {
    preview_message_attachment: { id: "notes", name: "notes.txt", kind: "text", media_type: "text/plain",
      content: "前🌟缀\n目标文本\n结尾", data_url: null },
  });
  await page.getByRole("button", { name: "notes.txt", exact: true }).click();
  const inspector = page.getByRole("dialog", { name: "notes.txt", exact: true });
  await expect(inspector.getByText("目标文本", { exact: false })).toBeVisible();
  await inspector.locator("pre").evaluate((element) => {
    const selection = window.getSelection();
    const range = document.createRange();
    range.setStart(element.firstChild, 5);
    range.setEnd(element.firstChild, 9);
    selection.removeAllRanges();
    selection.addRange(range);
    document.dispatchEvent(new Event("selectionchange"));
  });
  await inspector.getByRole("button", { name: "Reference selected text", exact: true }).click();
  await expect(page.getByRole("region", { name: "Referenced attachments" }).getByText("Characters 4–8")).toBeVisible();
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__referenceRequests.length)).toBe(1);
  expect(await page.evaluate(() => window.__referenceRequests[0].attachment_references))
    .toEqual([{ attachment_id: "notes", source_start: 4, source_end: 8 }]);
});
