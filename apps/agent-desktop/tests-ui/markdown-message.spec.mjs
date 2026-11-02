import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

test("assistant Markdown is rendered safely and remote images require one global approval", async ({ page }) => {
  test.setTimeout(35_000);
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"], { origin: "http://127.0.0.1:5173" });
  let imageRequests = 0;
  await page.route("https://images.example/*.png", async (route) => {
    imageRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: "image/png",
      body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64"),
    });
  });

  const now = "2026-09-15T00:00:00+00:00";
  const markdown = [
    "# Release notes",
    "",
    "- Added **Markdown** rendering",
    "- Kept user text safe",
    "",
    "| Area | Status |",
    "| --- | --- |",
    "| Chat | Ready |",
    "",
    "```ts",
    "const ready = true;",
    "```",
    "",
    "Inline math: $x^2 + y^2$",
    "",
    "```mermaid",
    "graph TD",
    "  A[Start] --> B[Done]",
    "```",
    "",
    "![Architecture](https://images.example/test.png)",
    "![Diagram](https://images.example/diagram.png)",
    "",
    "<script>window.markdownWasUnsafe = true</script>",
  ].join("\n");
  const conversation = {
    id: "markdown-chat", kind: "standard_chat", title: "Markdown test", category: "coding",
    resolved_category: "coding", chat_group_id: null, project_id: null, sort_order: 0,
    derived_from_conversation_id: null, derived_through_message_id: null, derived_at: null,
    copied_from_conversation_id: null, copied_through_message_id: null, copied_at: null,
    provider_profile_id: null, model_id: null, created_at: now, updated_at: now, deleted_at: null,
    messages: [
      { id: "user-1", conversation_id: "markdown-chat", role: "user", content: "# keep this plain", event_type: null, source_message_id: null, created_at: now },
      { id: "assistant-1", conversation_id: "markdown-chat", role: "assistant", content: markdown, event_type: null, source_message_id: null, created_at: now },
    ],
  };
  await installTauriMock(page, {
    list_conversations: [conversation],
    get_conversation: conversation,
  });

  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "Markdown test", exact: true }).click();

  await expect(page.getByRole("heading", { name: "Release notes" })).toBeVisible();
  await expect(page.locator(".markdown-table-scroll table")).toBeVisible();
  await expect(page.locator(".markdown-code-block")).toContainText("const ready = true;");
  await expect(page.locator(".markdown-code-block .token").first()).toBeVisible();
  await expect(page.locator(".katex").first()).toBeVisible();
  await expect(page.locator(".mermaid-diagram img")).toBeVisible({ timeout: 15_000 });
  await expect(page.locator(".chat-message--user h1")).toHaveCount(0);
  await expect(page.locator(".chat-message--assistant script")).toHaveCount(0);
  expect(await page.evaluate(() => window.markdownWasUnsafe)).toBeUndefined();
  const copyMarkdown = page.getByRole("button", { name: "Copy Markdown" });
  await expect(copyMarkdown).toBeVisible();
  await copyMarkdown.click();
  await expect(page.getByRole("button", { name: "Copied" })).toBeVisible();
  expect((await page.evaluate(() => navigator.clipboard.readText())).replaceAll("\r\n", "\n")).toBe(markdown);
  await expect(page.locator(".markdown-image")).toHaveCount(0);
  expect(imageRequests).toBe(0);

  await page.getByRole("button", { name: "Allow all remote images" }).first().click();
  await expect(page.locator(".markdown-image")).toHaveCount(2);
  await expect.poll(() => imageRequests).toBe(2);
});
