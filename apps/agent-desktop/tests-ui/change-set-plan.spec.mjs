import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

async function openPlan(page, withPlan = true) {
  const steps = [
    { operation_id: "dir-root", mutation: { kind: "mkdir", path: "new" }, depends_on: [], preconditions: {} },
    { operation_id: "dir-sub", mutation: { kind: "mkdir", path: "new/sub" }, depends_on: ["dir-root"], preconditions: {} },
    { operation_id: "write-a", mutation: { kind: "create_text", path: "new/sub/a.txt", content: "A" }, depends_on: ["dir-sub"], preconditions: {} },
    { operation_id: "write-b", mutation: { kind: "create_text", path: "b.txt", content: "B" }, depends_on: [], preconditions: {} },
  ];
  const changeSet = { id: "change-plan", operation_id: "op-plan", run_id: "run-plan", conversation_id: "work-1",
    project_id: "project-1", title: "Create planned files", risk: "medium", approval_mode: "human",
    mutations: steps.map((step) => step.mutation), status: "waiting_user", expires_at_epoch_ms: 9999999999999,
    recovery_point_id: null, undo_state: "unavailable",
    ...(withPlan ? { plan: { plan_id: "plan-immutable", plan_hash: "hash-approved-plan", policy_version: 1,
      root_generation: 4, steps, effective_risk: "medium" } } : {}) };
  const { work } = await installTauriMock(page, { list_agent_change_sets: [changeSet],
    get_conversation_model_run: { run: null }, list_tool_operations: { operations: [] }, resolve_agent_change_set: null });
  await page.addInitScript(() => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    window.__planApprovals = [];
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "resolve_agent_change_set") window.__planApprovals.push(args);
      return invoke(command, args);
    };
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: work.title, exact: true }).click();
  return page.locator(".change-set-card");
}

test("approval shows required directories and sends only the selected dependency closure and immutable plan", async ({ page }) => {
  const card = await openPlan(page);
  await expect(card.getByText("hash-approved-plan", { exact: true })).toBeVisible();
  await expect(card.getByText("Required by selected files", { exact: true })).toHaveCount(2);
  await expect(card.getByRole("checkbox", { name: "Select new", exact: true })).toBeChecked();
  await card.getByRole("checkbox", { name: "Select new/sub/a.txt", exact: true }).uncheck();
  await expect(card.getByRole("checkbox", { name: "Select new", exact: true })).not.toBeChecked();
  await expect(card.getByRole("checkbox", { name: "Select new/sub", exact: true })).not.toBeChecked();
  await card.getByRole("checkbox", { name: "Select new/sub/a.txt", exact: true }).check();
  await card.getByRole("checkbox", { name: "Select b.txt", exact: true }).uncheck();
  await card.getByRole("button", { name: "Approve 3 selected", exact: true }).click();
  expect(await page.evaluate(() => window.__planApprovals)).toEqual([{
    changeSetId: "change-plan", approve: true, planId: "plan-immutable", planHash: "hash-approved-plan",
    approvedOperationIds: ["dir-root", "dir-sub", "write-a"],
  }]);
});

test("legacy waiting records require a new plan instead of granting indexed approval", async ({ page }) => {
  const card = await openPlan(page, false);
  await expect(card.getByText("This request needs a new plan before approval. Ask the Agent to propose the changes again.")).toBeVisible();
  await expect(card.getByRole("button", { name: /^Approve/ })).toHaveCount(0);
  expect(await page.evaluate(() => window.__planApprovals)).toEqual([]);
});
