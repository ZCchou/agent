import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

test("fixed text model retains the image and shows guidance while generating", async ({ page }) => {
  const now = "2026-10-24T00:00:00Z";
  const image = { id: "saved-image", name: "image.png", kind: "image", media_type: "image/png",
    byte_size: 68, sha256: "image-hash", storage_key: "message-attachments/image.png", width: 1, height: 1 };
  const chat = { id: "image-chat", kind: "standard_chat", title: "Image fallback", category: "casual",
    resolved_category: "casual", project_id: null, chat_group_id: null, sort_order: 0,
    provider_profile_id: "builtin-demo", model_id: "demo", model_selection_mode: "fixed",
    created_at: now, updated_at: now, deleted_at: null, messages: [] };
  const user = { id: "user-image", conversation_id: chat.id, role: "user", content: "Explain this image",
    created_at: now, attachments: [image], attachment_rejections: [] };
  const run = { id: "image-run", conversation_id: chat.id, user_message_id: user.id, status: "generating",
    provider_profile_id: "builtin-demo", model_id: "demo", draft: { content: "" }, created_at: now, updated_at: now };
  await installTauriMock(page, { list_conversations: [chat], get_conversation: chat,
    get_conversation_model_run: { run: null }, connect_model_provider: null,
    import_clipboard_image: { accepted: [image], rejected: [] },
    preview_staged_attachment: { ...image, data_url: "data:image/png;base64,iVBORw0KGgo=", content: null },
    preview_message_attachment: { ...image, data_url: "data:image/png;base64,iVBORw0KGgo=", content: null },
    start_model_run: { status: "accepted", conversation: { ...chat, messages: [user] }, user_message: user, run,
      execution_plan: { id: "image-plan", degraded_capabilities: [{ capability_id: "vision_input", status: "unavailable",
        requested_level: "required", reason_code: "model_vision_unsupported", fallback_mode: "text_without_image" }] } },
    get_model_run: { run } });
  await page.addInitScript(() => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    window.__imageSends = [];
    window.__TAURI_INTERNALS__.invoke = (command, args) => {
      if (command === "start_model_run") window.__imageSends.push(args.request);
      return invoke(command, args);
    };
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: chat.title, exact: true }).click();
  const editor = page.getByRole("textbox", { name: "Ask agent..." });
  await editor.fill(user.content);
  await editor.evaluate((element) => {
    const data = new DataTransfer();
    data.items.add(new File([new Uint8Array([137, 80, 78, 71])], "image.png", { type: "image/png" }));
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
  });
  await expect(page.locator(".attachment-chip")).toHaveCount(1);
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.locator(".chat-message--user")).toContainText(user.content);
  await expect(page.locator(".chat-message--user")).toContainText(image.name);
  await expect(page.locator(".chat-capability-notice")).toContainText("Images were not read for this reply");
  await expect(page.locator(".chat-capability-notice")).toContainText("paste the text in the image");
  await expect(page.locator(".chat-capability-notice")).not.toContainText("vision_input");
  await expect(page.locator(".chat-error")).toHaveCount(0);
  const sends = await page.evaluate(() => window.__imageSends);
  expect(sends).toHaveLength(1);
  expect(sends[0].selection).toMatchObject({ mode: "fixed", model_id: "demo" });
  expect(sends[0].attachment_staging_ids).toEqual([image.id]);
  await expect(page.getByRole("button", { name: "Stop generating", exact: true })).toBeVisible();
});
