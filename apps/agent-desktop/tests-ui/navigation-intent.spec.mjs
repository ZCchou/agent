import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

test("navigation exposes implemented capabilities and profile opens settings", async ({ page }) => {
  await installTauriMock(page);
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "System features" }).click();
  await expect(page.getByRole("button", { name: "Abilities", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Reminders", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Global behavior instructions", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Global behavior instructions", level: 1 })).toBeVisible();
  await page.getByRole("button", { name: "Profile settings" }).click();
  await expect(page.getByRole("heading", { name: "Settings", level: 1 })).toBeVisible();
});

test("a new chat explains its default model before the conversation exists", async ({ page }) => {
  await installTauriMock(page);
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await expect(page.locator(".top-bar")).toContainText("Follow default: Built-in Demo / demo");
  await expect(page.locator(".top-bar select:disabled")).toHaveCount(0);
});

test("global instructions retain the last saved value after a failed edit", async ({ page }) => {
  await installTauriMock(page);
  await page.goto("http://127.0.0.1:5173");
  await page.evaluate(() => {
    const original = window.__TAURI_INTERNALS__.invoke;
    let saved = { content: "Keep sources visible.", enabled: true, revision: 1,
      content_hash: "synthetic", default_version: "v1", updated_at: "2026-10-13" };
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "get_global_instructions") return { global_instructions: saved };
      if (command === "update_global_instructions") {
        if (saved.revision > 1) throw new Error("synthetic save failure");
        saved = { ...saved, ...args, revision: 2 };
        return { global_instructions: saved };
      }
      return original(command, args);
    };
  });
  await page.getByRole("button", { name: "System features" }).click();
  await page.getByRole("button", { name: "Global behavior instructions", exact: true }).click();
  const editor = page.getByRole("textbox", { name: "Global behavior instructions" });
  await expect(editor).toHaveValue("Keep sources visible.");
  await editor.fill("Lead with the conclusion.");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Revision: 2", { exact: true })).toBeVisible();
  await editor.fill("Unsaved change");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Save failed; the previous settings remain active.")).toBeVisible();
  await expect(editor).toHaveValue("Unsaved change");
  await page.getByRole("button", { name: "Profile settings" }).click();
  await page.getByRole("button", { name: "Global behavior instructions", exact: true }).click();
  await expect(editor).toHaveValue("Lead with the conclusion.");
});

test("a consumed new-project request is not replayed when opening a work's parent project", async ({ page }) => {
  await installTauriMock(page);
  await page.goto("http://127.0.0.1:5173");

  await page.getByTitle("New Project").click();
  await expect(page.getByRole("dialog", { name: "New Project" })).toBeVisible();
  await page.getByRole("button", { name: "Cancel" }).click();

  await page.getByRole("button", { name: "Implementation work", exact: true }).click();
  await page.getByRole("button", { name: "Project Alpha", exact: true }).click();

  await expect(page.getByRole("heading", { name: "Project Alpha", level: 2 })).toBeVisible();
  await expect(page.getByRole("dialog", { name: "New Project" })).toHaveCount(0);
});
