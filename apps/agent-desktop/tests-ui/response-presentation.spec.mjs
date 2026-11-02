import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

const now = "2026-10-25T00:00:00Z";
const publicText = "先读取 main.js，再检查数据集生成逻辑。";
const finalText = "## 已完成\n已添加螺旋、圆形和月牙数据集，验证通过。";
const message = (id, role, content, event_type = null) => ({ id, role, content, event_type,
  conversation_id: "work-1", source_message_id: null, created_at: now });
const progress = message("progress-1", "system_event", JSON.stringify({
  version: 1, run_id: "run-1", attempt_id: "attempt-1", round_no: 1, content: publicText,
}), "assistant_progress");

async function open(page, { active = false, failed = false, events = false, processText = publicText, language = "en" } = {}) {
  const savedProgress = { ...progress, content: JSON.stringify({ ...JSON.parse(progress.content), content: processText }) };
  const messages = [message("user-1", "user", "实现非线性数据集"), savedProgress];
  if (!active && !failed) messages.push(message("answer-1", "assistant", finalText));
  const { work } = await installTauriMock(page, {
    list_conversations: [{ id: "work-1", project_id: "project-1", kind: "project_work", title: "Dataset work", category: null,
      resolved_category: "coding", chat_group_id: null, sort_order: 0, created_at: now, updated_at: now, messages }],
    get_conversation: { id: "work-1", project_id: "project-1", kind: "project_work", title: "Dataset work", category: null,
      resolved_category: "coding", messages },
    get_conversation_model_run: { run: active || failed ? {
      id: "run-1", conversation_id: "work-1", user_message_id: "user-1", status: failed ? "failed" : "generating",
      provider_profile_id: "builtin-demo", model_id: "demo", error_code: failed ? "stream_stalled" : null,
      error_message: failed ? "The model stopped producing output." : null,
      draft: { content: processText + "\n正在整理答复。", updated_at: now }, assistant_message: null,
    } : null },
  });
  await page.addInitScript((language) => {
    const original = window.__TAURI_INTERNALS__.invoke;
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "preview_project_file") return { path: "src/main.js", kind: "text", content: "Dataset generator source", truncated: false };
      const result = await original(command, args);
      return command === "get_app_settings" ? { ...result, general: { ...result.general, interface_language: language } } : result;
    };
  }, language);
  if (events) await page.addInitScript(() => {
    const original = window.__TAURI_INTERNALS__.invoke;
    const listeners = new Map();
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "plugin:event|listen") listeners.set(args.event, args.handler);
      return original(command, args);
    };
    window.__emitPresentation = (event_type, data) => window[`_${listeners.get("model-run-event")}`]({
      event: "model-run-event", id: 1, payload: { run_id: "run-1", event_type, timestamp: "2026-10-25", data },
    });
  });
  await page.setViewportSize({ width: 1000, height: 680 });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "Dataset work", exact: true }).click();
  return work;
}

test("completed work keeps public progress collapsed above a normal final answer after reload", async ({ page }) => {
  await open(page);
  const activity = page.locator(".run-activity");
  await expect(activity).toHaveCount(1);
  await expect(activity.getByRole("button").first()).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByText(publicText, { exact: true })).toHaveCount(0);
  await expect(page.locator(".chat-message--assistant .markdown-body")).toContainText("已添加螺旋、圆形和月牙数据集");
  await activity.getByRole("button").first().click();
  await expect(activity).toContainText(publicText);
  await expect(activity).not.toContainText("已添加螺旋、圆形和月牙数据集");
  await page.screenshot({ path: "../../.artifacts/collapsed-work-process-2026-10-25/completed-expanded.png" });
  await page.reload();
  await page.getByRole("button", { name: "Dataset work", exact: true }).click();
  await expect(activity.getByRole("button").first()).toHaveAttribute("aria-expanded", "false");
  await page.screenshot({ path: "../../.artifacts/collapsed-work-process-2026-10-25/completed-folded.png" });
});

test("saved process retains project file preview actions", async ({ page }) => {
  await open(page, { processText: "检查 [数据集源码](src/main.js)。" });
  await page.locator(".run-activity__summary").click();
  await page.getByRole("button", { name: "数据集源码", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "src/main.js" })).toContainText("Dataset generator source");
});

test("Chinese process and final answer remain contained at the minimum window width", async ({ page }) => {
  await open(page, { language: "zh-CN" });
  await page.setViewportSize({ width: 900, height: 600 });
  const toggle = page.getByRole("button", { name: "工作过程", exact: true });
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("heading", { name: "已完成", exact: true })).toBeVisible();
  await page.screenshot({ path: "../../.artifacts/collapsed-work-process-2026-10-25/chinese-folded.png" });
  await toggle.click();
  await expect(page.getByText(publicText, { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "../../.artifacts/collapsed-work-process-2026-10-25/chinese-expanded.png" });
});

test("expanding long saved or live process keeps its beginning on screen", async ({ page }) => {
  for (const active of [false, true]) {
    await open(page, { active, processText: "过程开头\n\n" + "正在检查数据集。\n\n".repeat(150) });
    const toggle = page.locator(active ? ".chat-run-message .work-process__toggle" : ".run-activity__summary");
    await expect(toggle).toBeVisible();
    const before = (await toggle.boundingBox()).y;
    await toggle.click();
    await expect(page.getByText("过程开头", { exact: true }).last()).toBeVisible();
    await expect.poll(async () => Math.abs((await toggle.boundingBox()).y - before)).toBeLessThan(4);
  }
});

test("live public narration is folded and can be expanded while Stop remains available", async ({ page }) => {
  await open(page, { active: true });
  const live = page.locator(".chat-run-message");
  await expect(page.getByRole("button", { name: "Stop generating" })).toBeVisible();
  await expect(live.locator(".markdown-body")).toHaveCount(0);
  const toggle = live.getByRole("button", { name: "Work process", exact: true });
  await toggle.click();
  await expect(live.locator(".markdown-body")).toContainText(publicText);
  await toggle.press("Enter");
  await expect(live.locator(".markdown-body")).toHaveCount(0);
});

test("failed runs keep an explicit cause outside a collapsed incomplete draft", async ({ page }) => {
  await open(page, { failed: true });
  const draft = page.locator(".chat-message--draft");
  await expect(draft.locator("strong").first()).not.toBeEmpty();
  await expect(draft.locator(".markdown-body")).toHaveCount(0);
  await expect(draft.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
  await draft.getByRole("button", { name: "Work process", exact: true }).click();
  await expect(draft.locator(".markdown-body")).toContainText(publicText);
});

test("stream completion installs progress before the final answer and copies only that answer", async ({ page }) => {
  await open(page, { active: true, events: true });
  await expect(page.getByRole("button", { name: "Stop generating" })).toBeVisible();
  await page.evaluate(({ finalText, progress }) => {
    Object.defineProperty(navigator, "clipboard", { configurable: true,
      value: { writeText: async (value) => { window.__copiedAnswer = value; } } });
    window.__emitPresentation("run.completed", { message: { id: "answer-1", role: "assistant", content: finalText,
      conversation_id: "work-1", event_type: null, source_message_id: null, created_at: "2026-10-25" },
      progress_messages: [progress], execution_summary_message: null });
  }, { finalText, progress });
  await expect(page.getByRole("button", { name: "Stop generating" })).toHaveCount(0);
  await expect(page.locator(".chat-message--assistant .markdown-body")).toContainText("已添加螺旋、圆形和月牙数据集");
  await expect(page.locator(".run-activity")).toHaveCount(1);
  await expect(page.locator(".run-activity__summary")).toHaveAttribute("aria-expanded", "false");
  await page.getByRole("button", { name: "Copy Markdown", exact: true }).click();
  expect(await page.evaluate(() => window.__copiedAnswer)).toBe(finalText);
});
