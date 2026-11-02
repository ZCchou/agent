import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

async function openWork(page, failPermission = false) {
  await installTauriMock(page, {
    get_project_grant: { project_id: "project-1", directory: "C:/synthetic/project", active: true, root_generation: 1 },
    get_work_permission: { conversation_id: "work-1", project_id: "project-1", root_generation: 1, mode: "human" },
  });
  await page.addInitScript(({ failPermission }) => {
    const original = window.__TAURI_INTERNALS__.invoke;
    window.__permissionWrites = [];
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "get_work_permission" && failPermission) throw new Error("Synthetic load failure");
      if (command === "set_work_permission") {
        window.__permissionWrites.push(args);
        return { conversation_id: "work-1", project_id: "project-1", root_generation: 1, mode: args.mode };
      }
      return original(command, args);
    };
  }, { failPermission });
  await page.goto("http://127.0.0.1:5173");
  await page.getByText("Implementation work", { exact: true }).click();
  return page.getByRole("combobox").filter({ has: page.locator('option[value="open"]') });
}

test("an explicit mode choice applies directly without another permission dialog", async ({ page }) => {
  const dialogs = [];
  page.on("dialog", async (dialog) => { dialogs.push(dialog.message()); await dialog.dismiss(); });
  const mode = await openWork(page);
  await expect(mode).toHaveValue("human");
  await mode.selectOption("open");
  await expect(mode).toHaveValue("open");
  expect(dialogs).toEqual([]);
  expect(await page.evaluate(() => window.__permissionWrites)).toEqual([{ conversationId: "work-1", mode: "open" }]);
});

test("an unavailable permission store cannot be presented as a working mode", async ({ page }) => {
  const mode = await openWork(page, true);
  await expect(mode).toBeDisabled();
  await expect(page.getByText("Could not load the work mode. Reopen this work to retry.")).toBeVisible();
  expect(await page.evaluate(() => window.__permissionWrites)).toEqual([]);
});
