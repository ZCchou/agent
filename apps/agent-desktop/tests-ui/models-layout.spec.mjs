import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

for (const width of [900, 1002]) test(`local model settings stay aligned at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 723 });
  await installTauriMock(page);
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "System features" }).click();
  await page.getByRole("navigation").getByRole("button", { name: "Models", exact: true }).click();
  await expect(page.getByRole("button", { name: "选择 GGUF 文件", exact: true })).toBeVisible();
  const tabs = page.getByRole("tablist", { name: "模型配置" });
  const boxes = await tabs.getByRole("tab").evaluateAll(elements => elements.map(element => {
    const box = element.getBoundingClientRect();
    return { height: box.height, top: box.top, icons: element.querySelectorAll("svg").length };
  }));
  expect(boxes).toHaveLength(3);
  expect(new Set(boxes.map(box => box.height)).size).toBe(1);
  expect(new Set(boxes.map(box => box.top)).size).toBe(1);
  expect(boxes.every(box => box.icons === 1)).toBe(true);
  await expect(page.getByRole("button", { name: "Add provider", exact: true })).toHaveCount(0);
  await expect(page.getByLabel("API Key", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Extra online cost", { exact: true })).toHaveCount(0);
  const overflow = await page.locator("main").evaluate(element => element.scrollWidth - element.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
});
