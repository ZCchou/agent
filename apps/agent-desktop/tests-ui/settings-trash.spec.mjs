import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

test("Trash is a second-level settings page and only loads after it is opened", async ({ page }) => {
  const calls = [];
  await installTauriMock(page);
  await page.exposeFunction("recordInvoke", (command) => calls.push(command));
  await page.addInitScript(() => {
    const original = window.__TAURI_INTERNALS__.invoke;
    window.__TAURI_INTERNALS__.invoke = async (command, ...rest) => {
      await window.recordInvoke(command);
      return original(command, ...rest);
    };
  });
  await page.goto("http://127.0.0.1:5173");

  await page.getByRole("button", { name: "System features" }).click();
  await page.getByRole("navigation").getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Settings", level: 2 })).toBeVisible();
  await expect(page.getByRole("button", { name: "Open Trash" })).toBeVisible();
  const remoteImages = page.getByRole("checkbox", { name: /Allow loading/ });
  await expect(remoteImages).not.toBeChecked();
  await expect(remoteImages).toBeDisabled();
  expect(calls).not.toContain("update_remote_image_policy");
  expect(calls).not.toContain("list_trash");

  await page.getByRole("button", { name: "Open Trash" }).click();
  await expect(page.getByRole("heading", { name: "Trash", level: 2 })).toBeVisible();
  await expect(page.getByText("Deleted items")).toBeVisible();
  await expect.poll(() => calls.filter((command) => command === "list_trash").length).toBe(1);
});
