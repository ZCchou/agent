import { expect, test } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import { installTauriMock } from "./tauri-mock.mjs";

test("application pages retain the global typography and contain their content", async ({ page }) => {
  test.setTimeout(90_000);
  await installTauriMock(page, { get_global_instructions: { global_instructions: { content: "Use worked examples.", enabled: true, revision: 1 } }, list_skills: [{ id: "lesson", name: "neural-network-learning-guide", description: "A reusable guide for learning neural networks, including weights, biases and gradients.", enabled: true, implicit: true, error: null, path: "C:\\Users\\Example\\skills\\neural-network-learning-guide" }] });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "System features" }).click();
  const records = [];
  for (const viewport of [{ width: 1280, height: 850 }, { width: 1000, height: 680 }, { width: 900, height: 600 }]) {
    await page.setViewportSize(viewport);
    for (const [name, ready] of [["Models", ".models-default-card"], ["Skills", ".skill-card"], ["Global behavior instructions", ".global-instructions-editor:not(:disabled)"], ["Approvals", ".approvals-page"], ["Settings", ".settings-language-control select:not(:disabled)"], ["Diagnostics", ".diagnostics-core-card"]]) {
      await page.getByRole("navigation").getByRole("button", { name, exact: true }).click();
      await expect(page.locator("main")).toBeVisible();
      await expect(page.locator(ready)).toBeVisible();
      const stats = await page.evaluate(() => {
        const root = getComputedStyle(document.documentElement);
        const main = document.querySelector("main");
        const elements = [...main.querySelectorAll("h1,h2,h3,p,label,input,select,textarea,button,.ui-card")];
        const details = elements.map((e) => {
          const style = getComputedStyle(e), rect = e.getBoundingClientRect();
          return { tag: e.tagName, text: (e.textContent || e.getAttribute("aria-label") || "").slice(0, 75), className: e.className,
            font: style.fontFamily, size: style.fontSize, width: rect.width, left: rect.left, right: rect.right,
            scrollWidth: e.scrollWidth, clientWidth: e.clientWidth, padding: style.padding, overflowX: style.overflowX };
        }).filter((e) => e.width > 0);
        return { rootFont: root.fontFamily, bodyFont: getComputedStyle(document.body).fontFamily, main: { width: main.clientWidth, scroll: main.scrollWidth }, elements: details };
      });
      records.push({ name, viewport, ...stats });
      if (viewport.width === 1000) await page.screenshot({ path: `../../.artifacts/ui-consistency-2026-10-24/current-${name.replaceAll(" ", "-")}.png` });
    }
  }
  await writeFile("../../.artifacts/ui-consistency-2026-10-24/audit.json", JSON.stringify(records, null, 2));
  for (const record of records) {
    expect.soft(record.bodyFont, `${record.name}: document font must match the app font`).toBe(record.rootFont);
    expect.soft(record.main.scroll, `${record.name} at ${record.viewport.width}: horizontal overflow`).toBeLessThanOrEqual(record.main.width + 1);
    for (const element of record.elements.filter((e) => /^H[123]$/.test(e.tag))) {
      expect.soft(element.font, `${record.name}: ${element.tag} font family`).toBe(record.rootFont);
      expect.soft(parseFloat(element.size), `${record.name}: oversized ${element.tag}`).toBeLessThanOrEqual(element.tag === "H3" ? 16 : 24);
    }
  }
});

test("composer and model controls remain usable at the minimum desktop size", async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 680 });
  await installTauriMock(page, { list_skills: [] });
  await page.goto("http://127.0.0.1:5173");
  const editor = page.locator(".chat-composer textarea");
  await expect(editor).toBeVisible();
  const editorBox = await editor.boundingBox();
  const sendBox = await page.getByRole("button", { name: "Send", exact: true }).boundingBox();
  expect.soft(editorBox.width, "composer editor was squeezed into an icon column").toBeGreaterThan(200);
  expect.soft(Math.abs(editorBox.y + editorBox.height / 2 - sendBox.y - sendBox.height / 2), "send must share the editor row").toBeLessThan(4);
  const labelsFit = await page.locator("button.top-select").evaluateAll((buttons) => buttons.every((button) => {
    const box = button.getBoundingClientRect();
    const walker = document.createTreeWalker(button, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      if (!walker.currentNode.textContent.trim()) continue;
      const range = document.createRange(); range.selectNodeContents(walker.currentNode);
      const text = range.getBoundingClientRect();
      if (text.top < box.top || text.bottom > box.bottom) return false;
    }
    return true;
  }));
  expect.soft(labelsFit, "top bar label extends vertically beyond its button").toBe(true);
  await page.getByRole("button", { name: "System features" }).click();
  await page.getByRole("navigation").getByRole("button", { name: "Models", exact: true }).click();
  const copy = page.locator(".models-routing-card .models-default-card__copy");
  await expect(copy).toBeVisible();
  expect.soft((await copy.boundingBox()).width, "routing copy collapsed to a narrow text strip").toBeGreaterThanOrEqual(200);
  const inputs = await page.locator(".models-routing-card__controls input:not([type=checkbox]), .models-routing-card__controls select").evaluateAll((elements) => elements.map((e) => e.getBoundingClientRect().height));
  expect.soft(inputs.every((height) => height >= 32), "routing controls use native browser sizing").toBe(true);
  await page.getByRole("navigation").getByRole("button", { name: "Global behavior instructions", exact: true }).click();
  const card = page.locator(".global-instructions-card");
  await expect(card).toBeVisible();
  expect(parseFloat(await card.evaluate((e) => getComputedStyle(e).paddingLeft))).toBeGreaterThanOrEqual(16);
});

test("stacked model settings do not turn horizontal flex bases into empty vertical space", async ({ page }) => {
  await page.setViewportSize({ width: 720, height: 680 });
  await installTauriMock(page);
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "System features", exact: true }).click();
  await page.getByRole("navigation").getByRole("button", { name: "Models", exact: true }).click();
  const copy = page.locator(".models-routing-card .models-default-card__copy");
  await expect(copy).toBeVisible();
  expect((await copy.boundingBox()).height).toBeLessThan(160);
});

for (const locale of ["en", "zh-CN"]) test(`long content remains contained with a wide sidebar (${locale})`, async ({ page }) => {
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 1000, height: 680 });
  await installTauriMock(page, {
    list_skills: [{ id: "lesson", name: "n".repeat(64), description: "学习神经网络并解释原理。".repeat(12), enabled: true, implicit: true, error: null, path: "C:\\Example\\" + "long-directory-name\\".repeat(10) }],
    get_project_grant: { project_id: "project-1", directory: "C:\\Projects\\" + "long-folder-name\\".repeat(12), root_generation: 1 },
    get_project_model_scope: { project_id: "project-1", policy: "inherit_global", models: [] },
    list_project_knowledge: { knowledge: [{ id: "knowledge-1", project_id: "project-1", title: "Project conventions / 项目约定", content: "Explain results with concrete examples. 解释结果时给出具体例子。".repeat(5), knowledge_type: "convention", pinned: false }] },
  });
  await page.addInitScript(({ locale }) => {
    const original = window.__TAURI_INTERNALS__.invoke;
    window.__TAURI_INTERNALS__.invoke = async (command, ...args) => {
      const result = await original(command, ...args);
      if (command === "get_app_settings") {
        result.general.interface_language = locale;
        result.sidebar.width_px = 420;
        const provider = result.models.providers[0];
        provider.name = "Model service / 模型服务".repeat(4);
        provider.models[0].id = "a-long-model-identifier-".repeat(5);
        result.models.default_model_id = provider.models[0].id;
      }
      if (command === "get_conversation") {
        result.title = "A long conversation title / 很长的对话标题".repeat(4);
        result.provider_profile_id = "builtin-demo";
        result.model_id = "a-long-model-identifier-".repeat(5);
        result.model_selection_mode = "fixed";
      }
      if (command === "list_conversations") {
        result[0].title = "A long conversation title / 很长的对话标题".repeat(4);
        result[0].provider_profile_id = "builtin-demo";
        result[0].model_id = "a-long-model-identifier-".repeat(5);
        result[0].model_selection_mode = "fixed";
      }
      if (command === "list_projects") result[0].name = "Project Alpha / " + "没有空格的超长项目名称".repeat(5);
      return result;
    };
  }, { locale });
  await page.goto("http://127.0.0.1:5173");
  const zh = locale === "zh-CN";
  await page.getByRole("button", { name: zh ? "系统功能" : "System features", exact: true }).click();
  async function contained(label, selector = "main") {
    const root = page.locator(selector);
    const overflow = await root.evaluate((e) => ({ client: e.clientWidth, scroll: e.scrollWidth }));
    expect.soft(overflow.scroll, label).toBeLessThanOrEqual(overflow.client + 1);
  }
  for (const [name, ready] of [[zh ? "模型" : "Models", ".models-default-card"], [zh ? "技能" : "Skills", ".skill-card"], [zh ? "全局行为指令" : "Global behavior instructions", ".global-instructions-editor"], [zh ? "设置" : "Settings", ".settings-language-card"], [zh ? "诊断" : "Diagnostics", ".diagnostics-core-card"]]) {
    await page.getByRole("navigation").getByRole("button", { name, exact: true }).click();
    await expect(page.locator(ready).first()).toBeVisible();
    await contained(`${locale}: ${name}`);
    await contained(`${locale}: header ${name}`, ".top-bar");
    if (name === (zh ? "模型" : "Models")) {
      await page.getByRole("button", { name: zh ? "管理模型" : "Manage models", exact: true }).click();
      const dialog = page.getByRole("dialog");
      await expect(dialog).toBeVisible();
      await contained(`${locale}: model catalog`, "[role=dialog]");
      await dialog.getByRole("button", { name: zh ? "关闭" : "Close", exact: true }).click();
    }
  }
  await page.getByRole("button", { name: /^Project Alpha \/ / }).click();
  await expect(page.locator(".project-detail__settings")).toBeVisible();
  await contained(`${locale}: project details`);
  const knowledge = page.locator(".project-knowledge article");
  await expect(knowledge).toBeVisible();
  expect.soft(parseFloat(await knowledge.evaluate((e) => getComputedStyle(e).paddingLeft)), "knowledge text needs card spacing").toBeGreaterThanOrEqual(14);
  await page.locator(".sidebar").getByRole("button", { name: /^A long conversation title/ }).click();
  await expect(page.locator(".top-bar__title")).toContainText("A long conversation title");
  await expect(page.locator(".chat-composer textarea")).toBeVisible();
  await contained(`${locale}: project work`);
  await contained(`${locale}: work composer`, ".chat-composer");
  await contained(`${locale}: work header`, ".top-bar");
  const workFieldsFit = await page.locator(".work-controls--compact").evaluate((root) => {
    const bounds = root.getBoundingClientRect();
    return [...root.querySelectorAll(".work-control-field")].every((field) => {
      const box = field.getBoundingClientRect();
      return box.right <= bounds.right + 1 && box.left >= bounds.left - 1;
    });
  });
  expect.soft(workFieldsFit, "work controls must not be clipped beyond the composer").toBe(true);
  const labelsOnOneLine = await page.locator(".work-control-field > span").evaluateAll((labels) => labels.every((label) => {
    return label.getBoundingClientRect().height <= parseFloat(getComputedStyle(label).lineHeight) + 1;
  }));
  expect.soft(labelsOnOneLine, "short work-control labels must not collapse into narrow text columns").toBe(true);
  await page.screenshot({ path: `../../.artifacts/ui-consistency-2026-10-24/long-work-${locale}.png` });
  await page.getByRole("navigation").getByRole("button", { name: zh ? "设置" : "Settings", exact: true }).click();
  await page.getByRole("button", { name: zh ? "打开" : "Open", exact: true }).click();
  await expect(page.getByText("离线模式已停用联网检索。")).toBeVisible();
  await expect(page.locator(".web-search-backend-form")).toHaveCount(0);
  await contained(`${locale}: offline search settings`);
  await page.screenshot({ path: `../../.artifacts/ui-consistency-2026-10-24/search-settings-${locale}.png` });
});
