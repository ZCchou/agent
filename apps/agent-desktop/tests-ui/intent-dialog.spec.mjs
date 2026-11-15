import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

async function openTask(page, patch = {}) {
  await installTauriMock(page);
  await page.goto(process.env.AGENT_UI_BASE_URL ?? "http://127.0.0.1:5173");
  await page.evaluate((patch) => {
    const original = window.__TAURI_INTERNALS__.invoke;
    window.intentCalls = [];
    window.intentTask = { id: "intent1", scope: "work-1", revision: 1, status: "waiting_user", request: {},
      attempts: 1, elapsed_seconds: 76, remaining_seconds: 14, timeout_seconds: 90, budget_seconds: 270,
      budget_remaining_seconds: 194, can_extend: true, can_retry: false, error: null, result: null,
      clarification: { summary: "Choose the report audience", questions: [{ id: "audience", question: "Who will read it?", options: ["Research team", "General public"] }] }, ...patch };
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command !== "intent_request") return original(command, args);
      window.intentCalls.push(args);
      if (args.action === "list") return { tasks: args.params.scope === "work-1" ? [window.intentTask] : [] };
      if (args.action === "respond") window.intentTask = { ...window.intentTask, revision: 2, status: "identifying", clarification: null, remaining_seconds: 80 };
      if (args.action === "cancel") window.intentTask = { ...window.intentTask, status: "cancelled" };
      return { task: window.intentTask };
    };
  }, patch);
  await page.getByRole("button", { name: "Implementation work", exact: true }).click();
}

test("clarification waits for explicit confirmation, dismisses and resumes with scoped answers", async ({ page }) => {
  await openTask(page);
  const dialog = page.getByRole("dialog", { name: "A little more information" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("radio").first()).not.toBeChecked();
  await expect(dialog.getByRole("button", { name: "Confirm and continue" })).toBeDisabled();
  await dialog.getByRole("radio", { name: "Research team" }).check();
  expect(await page.evaluate(() => window.intentCalls.filter((value) => value.action === "respond"))).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await page.getByRole("button", { name: "Continue providing information" }).click();
  await expect(dialog).toBeVisible();
  await dialog.getByRole("textbox", { name: "Your answer" }).fill("Research team with methods experience");
  await dialog.getByRole("button", { name: "Confirm and continue" }).click();
  await expect(dialog).not.toBeVisible();
  const replies = await page.evaluate(() => window.intentCalls.filter((value) => value.action === "respond"));
  expect(replies).toEqual([{ action: "respond", params: { task_id: "intent1", revision: 1, answers: { audience: "Research team with methods experience" } } }]);
});

test("long identification continues without a waiting dialog", async ({ page }) => {
  await page.setViewportSize({ width: 680, height: 480 });
  await openTask(page, { status: "identifying", clarification: null });
  const dialog = page.getByRole("dialog");
  await page.waitForTimeout(850);
  await expect(dialog).not.toBeVisible();
  expect(await page.evaluate(() => window.intentCalls.filter((value) => value.action === "extend"))).toEqual([]);
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole("button", { name: "Review intent task" })).toHaveCount(0);
});

test("technical retry errors keep clarification fields hidden", async ({ page }) => {
  await openTask(page, { status: "waiting_user", clarification: null, can_retry: true, can_extend: false,
    error: { code: "provider_timeout", message: "Provider did not respond" } });
  await expect(page.getByRole("dialog")).not.toBeVisible();
  await expect(page.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "Provider did not respond" })).toBeVisible();
});

test("clarification uses the available text width and preserves custom drafts", async ({ page }) => {
  await openTask(page);
  const dialog = page.getByRole("dialog", { name: "A little more information" });
  await expect(dialog).toBeVisible();
  const bounds = await dialog.locator("header > div").boundingBox();
  expect(bounds.width).toBeGreaterThan(300);
  const answer = dialog.getByRole("textbox", { name: "Your answer" });
  await answer.fill("Custom audience and long requirements");
  await dialog.getByRole("radio", { name: "Research team" }).check();
  await expect(answer).toHaveValue("Custom audience and long requirements");
  await dialog.getByRole("button", { name: "Confirm and continue" }).click();
  const replies = await page.evaluate(() => window.intentCalls.filter(value => value.action === "respond"));
  expect(replies[0].params.answers).toEqual({ audience: "Research team" });
});

for (const scale of [1.25, 1.5, 2]) test(`long clarification remains usable at ${scale} DPI`, async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 900, height: 500 }, deviceScaleFactor: scale });
  const page = await context.newPage();
  try {
    await openTask(page, { clarification: { summary: "审阅整份适航模板并保留原有章节。".repeat(12), questions: [1, 2, 3].map(id => ({
      id: String(id), question: `问题 ${id} ` + "需要补充的章节信息与试验参数。".repeat(8),
      options: ["F:/模型文件/很长的中文路径/".repeat(12), "使用当前项目资料"] })) } });
    const dialog = page.getByRole("dialog", { name: "A little more information" });
    await expect(dialog).toBeVisible();
    const dimensions = await dialog.evaluate(element => ({
      width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height,
      scroll: element.scrollWidth, client: element.clientWidth,
    }));
    expect(dimensions.width).toBeLessThanOrEqual(868);
    expect(dimensions.height).toBeLessThanOrEqual(468);
    expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.client + 1);
    await expect(dialog.getByRole("button", { name: "Confirm and continue" })).toBeInViewport();
    await dialog.getByRole("radio", { name: "使用当前项目资料", exact: true }).last().check();
    await expect(dialog.getByRole("button", { name: "Confirm and continue" })).toBeDisabled();
  } finally { await context.close(); }
});
