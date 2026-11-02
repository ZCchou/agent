import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";

const css = readFileSync(resolve("src/styles/components.css"), "utf8");

test("only the resource tree scrolls while fixed sidebar controls remain visible", async ({ page }) => {
  const resources = Array.from({ length: 48 }, (_, index) =>
    `<div class="resource-item-row"><button class="resource-item"><span>Conversation ${index}</span></button></div>`,
  ).join("");
  await page.setContent(`
    <style>
      :root { --color-sidebar:#f7f7f7; --color-border:#bbb; --color-border-soft:#ddd;
        --color-primary:#7657ff; --color-primary-soft:#eeeaff; --color-text:#222;
        --color-text-secondary:#555; --color-text-muted:#777; --color-surface:#fff; }
      * { box-sizing:border-box } body { margin:0 } ${css}
      .sidebar { width:280px; height:480px; }
      .sidebar-brand,.sidebar-new-chat,.sidebar-navigation,.core-status-card { flex:0 0 auto; }
      .sidebar-brand { height:45px; } .sidebar-new-chat { height:42px; }
      .sidebar-navigation { height:42px; } .core-status-card { height:50px; }
    </style>
    <aside class="sidebar">
      <div class="sidebar-brand">agent</div><button class="sidebar-new-chat">New Chat</button>
      <div class="sidebar-tree-frame"><nav class="sidebar-resource-tree">${resources}</nav></div>
      <nav class="sidebar-navigation sidebar-navigation--compact">Settings</nav>
      <button class="core-status-card">Core status</button>
    </aside>`);

  const tree = page.locator(".sidebar-resource-tree");
  const newChat = page.locator(".sidebar-new-chat");
  const status = page.locator(".core-status-card");
  const initialButton = await newChat.boundingBox();
  const initialStatus = await status.boundingBox();
  const metrics = await tree.evaluate((element) => ({
    scrollHeight: element.scrollHeight,
    clientHeight: element.clientHeight,
    overflowY: getComputedStyle(element).overflowY,
  }));
  expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight);
  expect(metrics.overflowY).toBe("auto");

  await tree.hover();
  await page.mouse.wheel(0, 700);
  await expect.poll(() => tree.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
  expect(await newChat.boundingBox()).toEqual(initialButton);
  expect(await status.boundingBox()).toEqual(initialStatus);
  await expect(newChat).toBeVisible();
  await expect(status).toBeVisible();
});
