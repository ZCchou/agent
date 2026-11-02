import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

test("the sidebar can be resized independently with its separator", async ({ page }) => {
  await installTauriMock(page);
  await page.goto("http://127.0.0.1:5173");

  const sidebar = page.locator(".sidebar");
  const separator = page.getByRole("separator", { name: "Resize sidebar" });
  await expect(separator).toHaveAttribute("aria-valuenow", "280");
  const handle = await separator.boundingBox();
  if (!handle) throw new Error("Resize separator has no layout box");

  await page.mouse.move(handle.x + handle.width / 2, handle.y + 120);
  await page.mouse.down();
  await page.mouse.move(handle.x + handle.width / 2 + 80, handle.y + 120, { steps: 5 });
  await page.mouse.up();

  await expect(separator).toHaveAttribute("aria-valuenow", "360");
  await expect.poll(async () => Math.round((await sidebar.boundingBox())?.width ?? 0)).toBe(360);
});
