import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

function changeSet(status, extra = {}) {
  return {
    id: "change-recovery", operation_id: "op-recovery", run_id: "run-recovery",
    conversation_id: "work-1", project_id: "project-1", title: "Update draft",
    risk: "medium", approval_mode: "human", mutations: [{ kind: "patch_text", path: "draft.txt", old_text: "old", new_text: "new" }],
    status, expires_at_epoch_ms: 0, recovery_point_id: null, undo_state: "unavailable",
    execution_id: "execution-1", recovery_error_code: "rollback_incomplete",
    recovery_affected_paths: ["draft.txt", "notes/other.txt"], ...extra,
  };
}

test("conflicted recovery keeps affected paths and error visible, then refreshes only from Host state", async ({ page }) => {
  const original = changeSet("rollback_conflicted");
  const { work } = await installTauriMock(page, {
    list_agent_change_sets: [original], get_conversation_model_run: { run: null },
    list_tool_operations: { operations: [] }, recover_agent_change_set: null,
  });
  await page.addInitScript((initial) => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    let current = initial;
    let attempts = 0;
    window.__recoveryRequests = [];
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "list_agent_change_sets") return [current];
      if (command === "recover_agent_change_set") {
        window.__recoveryRequests.push(args);
        attempts += 1;
        if (attempts === 1) throw new Error("recovery_unverified");
        current = { ...current, status: "rolled_back", recovery_error_code: null, recovery_affected_paths: [] };
        return current;
      }
      return invoke(command, args);
    };
  }, original);
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: work.title, exact: true }).click();
  const activity = page.locator(".run-activity");
  await expect(activity).not.toHaveClass(/run-activity--active/);
  await expect(activity).toHaveClass(/run-activity--failed/);
  await expect(activity.getByRole("button", { name: /Recovery needs attention/ })).toBeVisible();
  const card = activity.locator(".change-set-card");
  await expect(card).toBeVisible();
  await expect(card).toContainText("draft.txt");
  await expect(card).toContainText("notes/other.txt");
  await expect(card).toContainText("Recovery could not finish safely");
  await card.getByRole("button", { name: "Recheck recovery" }).click();
  await expect(card).toContainText("Recovery could not be verified");
  await expect(card).toContainText("notes/other.txt");
  await expect(card.getByRole("button", { name: "Recheck recovery" })).toBeEnabled();
  await card.getByRole("button", { name: "Recheck recovery" }).click();
  await expect(activity.getByRole("button", { name: /Changes rolled back/ })).toBeVisible();
  await expect(activity.locator(".change-set-card")).toContainText("Changes rolled back");
  await expect(activity.getByRole("button", { name: "Recheck recovery" })).toHaveCount(0);
  expect(await page.evaluate(() => window.__recoveryRequests)).toEqual([
    { changeSetId: "change-recovery" }, { changeSetId: "change-recovery" },
  ]);
});

test("uncertain execution remains actionable without a plan; completed legacy change has no recovery action", async ({ page }) => {
  const pending = changeSet("recovery_required", { recovery_error_code: "recovery_unverified", recovery_affected_paths: ["draft.txt"] });
  const completed = changeSet("completed", { id: "legacy-completed", title: "Legacy complete", execution_id: undefined, recovery_error_code: undefined,
    recovery_affected_paths: undefined, recovery_point_id: "point-1", undo_state: "available" });
  const { work } = await installTauriMock(page, {
    list_agent_change_sets: [pending, completed], get_conversation_model_run: { run: null }, list_tool_operations: { operations: [] },
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: work.title, exact: true }).click();
  const activity = page.locator(".run-activity");
  await expect(activity.getByRole("button", { name: /Recovery needs attention/ })).toBeVisible();
  const pendingCard = activity.locator(".change-set-card").filter({ hasText: "Update draft" });
  await expect(pendingCard).toContainText("Check disk state and attempt safe recovery");
  await expect(pendingCard.getByRole("button", { name: "Recheck recovery" })).toBeVisible();
  const completedCard = activity.locator(".change-set-card").filter({ hasText: "Legacy complete" });
  await expect(completedCard.getByRole("button", { name: "Recheck recovery" })).toHaveCount(0);
  await expect(completedCard.getByRole("button", { name: "Undo changes" })).toBeVisible();
});

test("legacy undo explains missing identity evidence without offering an unsafe retry", async ({ page }) => {
  const legacy = changeSet("completed", { title: "Legacy unverified", execution_id: undefined,
    recovery_error_code: undefined, recovery_affected_paths: undefined, recovery_point_id: "old-point",
    undo_state: "unavailable", undo_block_reason: "recovery_unverified" });
  const { work } = await installTauriMock(page, {
    list_agent_change_sets: [legacy], get_conversation_model_run: { run: null }, list_tool_operations: { operations: [] },
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: work.title, exact: true }).click();
  await page.locator(".run-activity__summary").click();
  const card = page.locator(".change-set-card");
  await expect(card).toContainText("this older record lacks directory identity evidence");
  await expect(card.getByRole("button", { name: "Undo changes" })).toHaveCount(0);
  await expect(card.getByRole("button", { name: "Recheck recovery" })).toHaveCount(0);
});
