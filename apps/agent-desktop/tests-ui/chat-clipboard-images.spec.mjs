import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aFz8AAAAASUVORK5CYII=";

async function openChat(page, delayed = false) {
  const now = "2026-10-24T00:00:00Z";
  const chat = { id: "clipboard-chat", kind: "standard_chat", title: "Clipboard images", category: "casual",
    resolved_category: "casual", chat_group_id: null, project_id: null, sort_order: 0,
    provider_profile_id: "builtin-demo", model_id: "demo", model_selection_mode: "fixed",
    created_at: now, updated_at: now, deleted_at: null, messages: [] };
  await installTauriMock(page, { list_conversations: [chat], get_conversation: chat,
    get_conversation_model_run: { run: null }, connect_model_provider: null, discard_staged_attachment: null,
    start_model_run: { status: "preflight_rejected", rejection: { reason_code: "no_compatible_execution_plan" } } });
  await page.addInitScript(({ delayed, png }) => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    window.__clipboardImports = []; window.__clipboardSends = []; window.__clipboardDiscards = [];
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "import_clipboard_image") {
        window.__clipboardImports.push(args);
        const result = { accepted: [{ id: `clip-${window.__clipboardImports.length}`, name: args.name, kind: "image", media_type: "image/png",
          byte_size: 68, sha256: "same-image-hash", storage_key: "message-attachments/clip-1.bin",
          width: 1, height: 1, model_storage_key: "message-attachments/clip-1.model.png", model_media_type: "image/png",
          model_byte_size: 68, model_sha256: "model-image-hash", model_width: 1, model_height: 1,
          normalization_version: "image-normalization-v1", artifacts: [] }], rejected: [] };
        return delayed ? new Promise((resolve) => { window.__finishClipboardImport = () => resolve(result); }) : result;
      }
      if (command === "preview_staged_attachment") return { id: args.attachmentId, kind: "image", name: "image.png",
        media_type: "image/png", data_url: `data:image/png;base64,${png}`, content: null };
      if (command === "discard_staged_attachment") window.__clipboardDiscards.push(args.attachmentId);
      if (command === "start_model_run") window.__clipboardSends.push(args.request);
      return invoke(command, args);
    };
  }, { delayed, png: PNG });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: chat.title, exact: true }).click();
  return page.getByRole("textbox", { name: "Ask agent..." });
}

async function paste(editor, text = "", image = true) {
  return editor.evaluate((element, { png, text, image }) => {
    const data = new DataTransfer();
    if (text) data.setData("text/plain", text);
    if (image) data.items.add(new File([Uint8Array.from(atob(png), (c) => c.charCodeAt(0))], "image.png", { type: "image/png" }));
    const event = new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true });
    element.dispatchEvent(event);
    return event.defaultPrevented;
  }, { png: PNG, text, image });
}

test("pasted image is previewed, removable and sent without text through staged attachments", async ({ page }) => {
  const editor = await openChat(page);
  expect(await paste(editor)).toBe(true);
  await expect(page.locator(".attachment-chip img")).toHaveAttribute("src", `data:image/png;base64,${PNG}`);
  expect(await page.evaluate(() => window.__clipboardImports)).toEqual([{ name: "image.png", dataBase64: PNG }]);
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__clipboardSends.length)).toBe(1);
  expect(await page.evaluate(() => window.__clipboardSends[0].attachment_staging_ids)).toEqual(["clip-1"]);
  await page.getByRole("button", { name: "Remove attachment image.png", exact: true }).click();
  await expect(page.locator(".attachment-chip")).toHaveCount(0);
  expect(await page.evaluate(() => window.__clipboardDiscards)).toContain("clip-1");
});

test("mixed image and text paste replaces the selection; plain text paste keeps browser defaults", async ({ page }) => {
  const editor = await openChat(page);
  await editor.fill("before AFTER");
  expect(await paste(editor, "ordinary text", false)).toBe(false);
  await editor.evaluate((element) => element.setSelectionRange(7, 12));
  await paste(editor, "caption");
  await expect(editor).toHaveValue("before caption");
  await expect(page.locator(".attachment-chip img")).toBeVisible();
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__clipboardSends.length)).toBe(1);
  expect(await page.evaluate(() => window.__clipboardSends[0].content)).toBe("before caption");
});

test("Enter and Send wait for image import to finish", async ({ page }) => {
  const editor = await openChat(page, true);
  await editor.fill("Explain this screenshot");
  await paste(editor);
  await expect.poll(() => page.evaluate(() => window.__clipboardImports.length)).toBe(1);
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeDisabled();
  await editor.press("Enter");
  expect(await page.evaluate(() => window.__clipboardSends)).toEqual([]);
  await page.evaluate(() => window.__finishClipboardImport());
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeEnabled();
  await editor.press("Enter");
  await expect.poll(() => page.evaluate(() => window.__clipboardSends.length)).toBe(1);
  expect(await page.evaluate(() => window.__clipboardSends[0].attachment_staging_ids)).toEqual(["clip-1"]);
});

test("late image import is discarded after leaving and returning to the conversation", async ({ page }) => {
  const editor = await openChat(page, true);
  await paste(editor);
  await expect.poll(() => page.evaluate(() => window.__clipboardImports.length)).toBe(1);
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await page.getByRole("button", { name: "Clipboard images", exact: true }).click();
  await page.evaluate(() => window.__finishClipboardImport());
  await expect.poll(() => page.evaluate(() => window.__clipboardDiscards)).toContain("clip-1");
  await expect(page.locator(".attachment-chip")).toHaveCount(0);
});

test("pasting the same image twice keeps one attachment and discards duplicate staging", async ({ page }) => {
  const editor = await openChat(page);
  await paste(editor);
  await expect(page.locator(".attachment-chip img")).toBeVisible();
  await paste(editor);
  await expect.poll(() => page.evaluate(() => window.__clipboardDiscards)).toContain("clip-2");
  await expect(page.locator(".attachment-chip")).toHaveCount(1);
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__clipboardSends.length)).toBe(1);
  expect(await page.evaluate(() => window.__clipboardSends[0].attachment_staging_ids)).toEqual(["clip-1"]);
});

test("oversized clipboard images explain the failure and keep the text draft usable", async ({ page }) => {
  const editor = await openChat(page);
  await editor.fill("Keep this draft");
  await editor.evaluate((element) => {
    const data = new DataTransfer();
    data.items.add(new File([new Uint8Array(10 * 1024 * 1024 + 1)], "large.png", { type: "image/png" }));
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
  });
  await expect(page.locator(".attachment-tray__error")).toContainText(/per-file limit/i);
  await expect(editor).toHaveValue("Keep this draft");
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeEnabled();
  expect(await page.evaluate(() => window.__clipboardImports)).toEqual([]);
});

test("a valid image does not hide the reason another pasted image was rejected", async ({ page }) => {
  const editor = await openChat(page);
  await editor.evaluate((element, png) => {
    const data = new DataTransfer();
    data.items.add(new File(["<svg></svg>"], "unsupported.svg", { type: "image/svg+xml" }));
    data.items.add(new File([Uint8Array.from(atob(png), (c) => c.charCodeAt(0))], "image.png", { type: "image/png" }));
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
  }, PNG);
  await expect(page.locator(".attachment-chip img")).toBeVisible();
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeEnabled();
  await expect(page.locator(".attachment-tray__error")).toContainText(/cannot be read/i);
});
