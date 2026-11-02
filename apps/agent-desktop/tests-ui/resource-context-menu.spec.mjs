import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

async function setup(page, { collapsed = false, language = "en" } = {}) {
  const { project, work } = await installTauriMock(page, {
    list_project_knowledge: { knowledge: [] }, get_project_model_scope: { policy: "inherit_global", models: [] },
  });
  const chat = { ...work, id: "chat-1", title: "Learning notes", kind: "standard_chat", project_id: null,
    category: "research", resolved_category: "research" };
  await page.addInitScript(({ project, work, chat, collapsed, language }) => {
    const original = window.__TAURI_INTERNALS__.invoke;
    let state = JSON.parse(localStorage.getItem("resource-menu-fixture") || "null") ?? {
      projects: [project, { ...project, id: "project-2", name: "Other project", sort_order: 1 }],
      conversations: [chat, work],
      sidebar: collapsed ? { collapsed_chat_group_ids: ["group-1"], collapsed_project_ids: ["project-2"] } : {},
    };
    window.__resourceWrites = [];
    window.__failRename = false;
    window.__failDelete = false;
    window.__directoryOpens = [];
    window.__missingDirectory = false;
    window.__failDirectoryOpen = false;
    const persist = () => localStorage.setItem("resource-menu-fixture", JSON.stringify(state));
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "get_project_grant") return window.__missingDirectory ? null : {
        project_id: args.projectId, directory: "C:\\Synthetic\\Project", active: true, root_generation: 1,
      };
      if (command === "open_project_root_directory") {
        window.__directoryOpens.push(args);
        if (window.__failDirectoryOpen) throw new Error("synthetic directory missing");
        return;
      }
      if (command === "get_app_settings") {
        const settings = await original(command, args);
        return { ...settings, general: { ...settings.general, interface_language: language }, sidebar: { ...settings.sidebar, ...state.sidebar } };
      }
      if (command === "update_sidebar_preferences") {
        state.sidebar = args.preferences;
        persist();
        return { ...(await original("get_app_settings")), sidebar: state.sidebar };
      }
      if (command === "list_projects") return structuredClone(state.projects);
      if (command === "list_conversations") return structuredClone(state.conversations);
      if (command === "get_conversation") return structuredClone(state.conversations.find((c) => c.id === args.conversationId));
      if (command === "list_chat_groups") return [{ id: "group-1", name: "Study group", sort_order: 0 }];
      if (command === "update_project" || command === "update_conversation") {
        window.__resourceWrites.push({ command, args });
        if (window.__failRename) throw new Error("synthetic save failure");
        if (command === "update_project") {
          const p = state.projects.find((p) => p.id === args.projectId);
          if (args.category !== p.category) throw new Error("Rename must preserve project category");
          p.name = args.name;
          persist();
          return structuredClone(p);
        }
        const c = state.conversations.find((c) => c.id === args.conversationId);
        if (args.category !== (c.kind === "standard_chat" ? "research" : null)) throw new Error("Incorrect rename category");
        c.title = args.title;
        persist();
        return structuredClone(c);
      }
      if (command === "project_delete_impact") {
        if (window.__failDelete) throw new Error("synthetic delete failure");
        return { work_count: 1, message_count: 2 };
      }
      if (command === "move_chat" || command === "move_work") {
        const c = state.conversations.find((c) => c.id === args.conversationId);
        if (command === "move_chat") c.chat_group_id = args.chatGroupId;
        else c.project_id = args.projectId;
        persist();
        return structuredClone(c);
      }
      if (command === "copy_work") {
        const source = state.conversations.find((c) => c.id === args.sourceConversationId);
        const copy = { ...source, id: "copy-1", project_id: args.projectId, title: "Copied experiment" };
        state.conversations.push(copy);
        persist();
        return structuredClone(copy);
      }
      if (command === "delete_conversation") {
        state.conversations = state.conversations.filter((c) => c.id !== args.conversationId);
        persist();
        return;
      }
      if (command === "delete_project") {
        state.projects = state.projects.filter((p) => p.id !== args.projectId);
        state.conversations = state.conversations.filter((c) => c.project_id !== args.projectId);
        persist();
        return;
      }
      return original(command, args);
    };
  }, { project, work, chat, collapsed, language });
  await page.setViewportSize({ width: 1000, height: 680 });
  await page.goto("http://127.0.0.1:5173");
  await expect(page.getByRole("button", { name: "Learning notes", exact: true })).toBeVisible();
}

async function renameFromMenu(page, target, value) {
  await target.click({ button: "right" });
  await page.getByRole("menuitem", { name: "Rename", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Rename", exact: true });
  await dialog.getByRole("textbox").fill(value);
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).toHaveCount(0);
}

test("right-click renames an inactive chat without switching conversation or losing a draft", async ({ page }) => {
  await setup(page);
  await page.getByRole("button", { name: "Implementation work", exact: true }).click();
  await page.locator(".chat-composer textarea").fill("Keep this draft");
  await renameFromMenu(page, page.getByRole("button", { name: "Learning notes", exact: true }), "  梯度学习笔记  ");
  await expect(page.getByRole("button", { name: "梯度学习笔记", exact: true })).toBeVisible();
  await expect(page.locator(".top-bar h1")).toHaveText("Implementation work");
  await expect(page.locator(".chat-composer textarea")).toHaveValue("Keep this draft");
  await page.reload();
  await expect(page.getByRole("button", { name: "梯度学习笔记", exact: true })).toBeVisible();
});

test("project menu opens the selected root without switching chats or clearing a draft", async ({ page }) => {
  await setup(page);
  await page.getByRole("button", { name: "Implementation work", exact: true }).click();
  await page.locator(".chat-composer textarea").fill("Keep directory-action draft");
  const target = page.getByRole("button", { name: "Other project", exact: true });
  await target.click({ button: "right" });
  await page.getByRole("menuitem", { name: "Open directory", exact: true }).click();
  await expect(page.getByRole("menu")).toHaveCount(0);
  expect(await page.evaluate(() => window.__directoryOpens)).toEqual([{ projectId: "project-2" }]);
  await expect(page.locator(".top-bar h1")).toHaveText("Implementation work");
  await expect(page.locator(".chat-composer textarea")).toHaveValue("Keep directory-action draft");
  await expect(target).toBeFocused();
});

test("project cards can open a directory even without a work conversation; chat menus omit it", async ({ page }) => {
  await setup(page);
  await page.getByRole("button", { name: "Learning notes", exact: true }).click({ button: "right" });
  await expect(page.getByRole("menuitem", { name: "Open directory", exact: true })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await page.getByTitle("Open projects", { exact: true }).click();
  await page.locator(".project-card").filter({ hasText: "Other project" }).click({ button: "right" });
  await page.getByRole("menuitem", { name: "Open directory", exact: true }).click();
  expect(await page.evaluate(() => window.__directoryOpens)).toEqual([{ projectId: "project-2" }]);
  await expect(page.locator(".projects-page .project-card")).toHaveCount(2);
});

test("a missing or unavailable directory stays actionable with an explanation and retry", async ({ page }) => {
  await setup(page);
  await page.evaluate(() => { window.__missingDirectory = true; });
  await page.getByRole("button", { name: "Project Alpha", exact: true }).click({ button: "right" });
  const open = page.getByRole("menuitem", { name: "Open directory", exact: true });
  await open.click();
  await expect(page.getByRole("menu").getByRole("alert")).toContainText("Authorize a work directory");
  expect(await page.evaluate(() => window.__directoryOpens)).toEqual([]);
  await page.evaluate(() => { window.__missingDirectory = false; window.__failDirectoryOpen = true; });
  await open.click();
  await expect(page.getByRole("menu").getByRole("alert")).toContainText("could not be opened");
  await expect(open).toBeEnabled();
  await page.evaluate(() => { window.__failDirectoryOpen = false; });
  await open.click();
  await expect(page.getByRole("menu")).toHaveCount(0);
});

test("Chinese project directory menu follows the global menu style", async ({ page }) => {
  await setup(page, { language: "zh-CN" });
  await page.getByRole("button", { name: "Project Alpha", exact: true }).click({ button: "right" });
  await expect(page.getByRole("menuitem", { name: "打开目录", exact: true })).toBeVisible();
  await page.screenshot({ path: "../../.artifacts/project-open-directory-2026-10-25/chinese-menu.png" });
});

test("project work right-click rename refreshes its active title and toolbar rename uses the latest name", async ({ page }) => {
  await setup(page);
  const work = page.getByRole("button", { name: "Implementation work", exact: true });
  await work.click();
  await renameFromMenu(page, work, "Network experiments");
  await expect(page.locator(".top-bar h1")).toHaveText("Network experiments");
  await page.locator(".chat-toolbar").getByRole("button", { name: "Rename", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Rename", exact: true }).getByRole("textbox")).toHaveValue("Network experiments");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Rename", exact: true })).toHaveCount(0);
});

test("project sidebar and detail rename share persisted names and deletion is only in actions", async ({ page }) => {
  await setup(page);
  const project = page.getByRole("button", { name: "Project Alpha", exact: true });
  await renameFromMenu(page, project, "Neural Lab");
  await page.getByRole("button", { name: "Neural Lab", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Neural Lab", level: 2 })).toBeVisible();
  await expect(page.locator(".projects-page .page-heading-actions").getByRole("button", { name: "Delete", exact: true })).toHaveCount(0);
  await expect(page.locator(".resource-group__title").filter({ hasText: "Neural Lab" }).getByTitle("Delete", { exact: true })).toHaveCount(0);
  await page.locator(".projects-page .page-heading-actions").getByRole("button", { name: "Rename", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Rename", exact: true });
  await dialog.getByRole("textbox", { name: "Project name", exact: true }).fill("神经网络实验室");
  await page.screenshot({ path: "../../.artifacts/resource-context-menus-2026-10-24/rename-project.png" });
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("heading", { name: "神经网络实验室", level: 2 })).toBeVisible();
  await expect(page.getByRole("button", { name: "神经网络实验室", exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button", { name: "神经网络实验室", exact: true })).toBeVisible();
});

test("project cards and work cards open the corresponding context menu without navigation", async ({ page }) => {
  await setup(page);
  await page.getByTitle("Open projects", { exact: true }).click();
  await renameFromMenu(page, page.locator(".project-card").filter({ hasText: "Project Alpha" }), "Card project");
  await expect(page.locator(".project-card").filter({ hasText: "Card project" })).toBeVisible();
  await page.locator(".project-card").filter({ hasText: "Card project" }).click();
  await renameFromMenu(page, page.locator(".project-card").filter({ hasText: "Implementation work" }), "Card work");
  await expect(page.getByRole("heading", { name: "Card project", level: 2 })).toBeVisible();
  await expect(page.locator(".project-card").filter({ hasText: "Card work" })).toBeVisible();
});

test("rename validates empty and unchanged input and keeps failed edits for retry", async ({ page }) => {
  await setup(page);
  await page.getByRole("button", { name: "Learning notes", exact: true }).click({ button: "right" });
  await page.getByRole("menuitem", { name: "Rename", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Rename", exact: true });
  const editor = dialog.getByRole("textbox");
  const save = dialog.getByRole("button", { name: "Save", exact: true });
  await expect(save).toBeDisabled();
  await editor.fill("   ");
  await expect(save).toBeDisabled();
  await editor.fill("Failed edit");
  await page.evaluate(() => { window.__failRename = true; });
  await save.click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await expect(editor).toHaveValue("Failed edit");
  await page.keyboard.press("Shift+Tab");
  expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
  await expect(page.getByRole("button", { name: "Learning notes", exact: true })).toBeVisible();
  await page.evaluate(() => { window.__failRename = false; });
  await editor.press("Enter");
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Failed edit", exact: true })).toBeVisible();
});

test("menus fit viewport edges, support keyboard opening and restore focus on Escape", async ({ page }) => {
  const errors = [];
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await setup(page);
  const row = page.getByRole("button", { name: "Implementation work", exact: true });
  await row.evaluate((node) => node.dispatchEvent(new MouseEvent("contextmenu", { clientX: 998, clientY: 678, bubbles: true })));
  const menu = page.getByRole("menu");
  await expect(menu).toBeVisible();
  const bounds = await menu.boundingBox();
  expect(bounds.x).toBeGreaterThanOrEqual(0);
  expect(bounds.y).toBeGreaterThanOrEqual(0);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(1000);
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(680);
  await page.screenshot({ path: "../../.artifacts/resource-context-menus-2026-10-24/work-menu.png" });
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await row.focus();
  await page.keyboard.press("Shift+F10");
  await expect(menu).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Rename", exact: true })).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("menuitem", { name: "Move to project", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(row).toBeFocused();
  await row.click({ button: "right" });
  await page.mouse.click(700, 80);
  await expect(menu).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("project menu deletion retains both confirmations and removes only the selected project", async ({ page }) => {
  await setup(page);
  const project = page.getByRole("button", { name: "Project Alpha", exact: true });
  await project.click();
  await page.locator(".projects-page .page-heading-actions").getByRole("button", { name: "More actions", exact: true }).click();
  page.once("dialog", (d) => d.dismiss());
  await page.getByRole("menuitem", { name: "Delete", exact: true }).click();
  await expect(project).toBeVisible();
  await page.keyboard.press("Escape");
  await project.click({ button: "right" });
  page.once("dialog", async (d) => {
    page.once("dialog", (second) => second.dismiss());
    await d.accept();
  });
  await page.getByRole("menuitem", { name: "Delete", exact: true }).click();
  await expect(project).toBeVisible();
  await expect(page.getByRole("button", { name: "Implementation work", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await project.click({ button: "right" });
  const confirmations = [];
  page.on("dialog", async (d) => { confirmations.push(d.message()); await d.accept(); });
  await page.getByRole("menuitem", { name: "Delete", exact: true }).click();
  await expect(project).toHaveCount(0);
  expect(confirmations).toHaveLength(2);
  await expect(page.getByRole("button", { name: "Implementation work", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Learning notes", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Other project", exact: true })).toBeVisible();
});

test("a failed project delete keeps its error and menu inside the viewport", async ({ page }) => {
  await setup(page);
  await page.evaluate(() => { window.__failDelete = true; });
  await page.getByRole("button", { name: "Project Alpha", exact: true }).evaluate((node) => node.dispatchEvent(new MouseEvent("contextmenu", { clientX: 998, clientY: 678, bubbles: true })));
  await page.getByRole("menuitem", { name: "Delete", exact: true }).click();
  await expect(page.getByRole("menu").getByRole("alert")).toBeVisible();
  await expect.poll(async () => {
    const bounds = await page.getByRole("menu").boundingBox();
    return bounds.y + bounds.height;
  }).toBeLessThanOrEqual(680);
  await page.screenshot({ path: "../../.artifacts/resource-context-menus-2026-10-24/menu-error.png" });
});

test("shared menus preserve chat move and project work move/copy/delete actions", async ({ page }) => {
  await setup(page, { collapsed: true });
  await page.getByRole("button", { name: "Learning notes", exact: true }).click({ button: "right" });
  await page.getByRole("menuitem", { name: "Move to chat group", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Manage conversation", exact: true });
  await dialog.getByRole("combobox").selectOption("group-1");
  await dialog.getByRole("button", { name: "Move", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".resource-group").filter({ has: page.getByRole("button", { name: "Study group", exact: true }) })
    .getByRole("button", { name: "Learning notes", exact: true })).toBeVisible();
  const work = page.getByRole("button", { name: "Implementation work", exact: true });
  await work.click({ button: "right" });
  await page.getByRole("menuitem", { name: "Move to project", exact: true }).click();
  await dialog.getByRole("combobox").selectOption("project-2");
  page.once("dialog", (d) => d.accept());
  await dialog.getByRole("button", { name: "Move", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  const targetGroup = page.locator(".resource-group").filter({ has: page.getByRole("button", { name: "Other project", exact: true }) });
  await expect(targetGroup.getByRole("button", { name: "Implementation work", exact: true })).toBeVisible();
  await work.click({ button: "right" });
  await page.getByRole("menuitem", { name: "Copy to project", exact: true }).click();
  await dialog.getByRole("combobox").selectOption("project-1");
  await dialog.getByRole("button", { name: "Copy to project", exact: true }).click();
  await expect(page.locator(".top-bar h1")).toHaveText("Copied experiment");
  await page.getByRole("button", { name: "Copied experiment", exact: true }).click({ button: "right" });
  await page.getByRole("menuitem", { name: "Delete", exact: true }).click();
  page.once("dialog", (d) => d.accept());
  await dialog.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.getByRole("button", { name: "Copied experiment", exact: true })).toHaveCount(0);
  await expect(work).toBeVisible();
});

test("pending rename stays modal and rapid submits produce one saved change", async ({ page }) => {
  await setup(page);
  await page.evaluate(() => {
    const original = window.__TAURI_INTERNALS__.invoke;
    window.__renameRequests = 0;
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "update_conversation") {
        window.__renameRequests++;
        await new Promise((resolve) => { window.__releaseRename = resolve; });
      }
      return original(command, args);
    };
  });
  await page.getByRole("button", { name: "Learning notes", exact: true }).click({ button: "right" });
  await page.getByRole("menuitem", { name: "Rename", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Rename", exact: true });
  await dialog.getByRole("textbox").fill("One saved rename");
  await dialog.locator("form").evaluate((form) => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  await expect(dialog.getByRole("textbox")).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Tab");
  expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
  await page.evaluate(() => { window.__releaseRename(); });
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "One saved rename", exact: true })).toBeVisible();
  expect(await page.evaluate(() => window.__renameRequests)).toBe(1);
});

test("renaming current work refreshes the title suggested when saving project knowledge", async ({ page }) => {
  await setup(page);
  await page.evaluate(() => {
    const original = window.__TAURI_INTERNALS__.invoke;
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      const result = await original(command, args);
      if (command === "get_conversation" && args.conversationId === "work-1") return { ...result,
        messages: [{ id: "answer-1", conversation_id: "work-1", role: "assistant", content: "A useful explanation.", event_type: null, created_at: "2026-10-24" }] };
      return result;
    };
  });
  const work = page.getByRole("button", { name: "Implementation work", exact: true });
  await work.click();
  await expect(page.getByText("A useful explanation.", { exact: true })).toBeVisible();
  await renameFromMenu(page, work, "Updated knowledge source");
  const prompt = page.waitForEvent("dialog").then(async (dialog) => {
    const value = dialog.defaultValue();
    await dialog.dismiss();
    return value;
  });
  await page.getByRole("button", { name: "Suggest project knowledge", exact: true }).click();
  expect(await prompt).toBe("Updated knowledge source");
});

test("Chinese actions and an 80-character project name stay contained", async ({ page }) => {
  await setup(page, { language: "zh-CN" });
  await page.getByRole("button", { name: "Project Alpha", exact: true }).click({ button: "right" });
  await page.getByRole("menuitem", { name: "重命名", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "重命名", exact: true });
  const longName = "神经网络".repeat(20);
  await dialog.getByRole("textbox").fill(longName);
  await page.screenshot({ path: "../../.artifacts/resource-context-menus-2026-10-24/rename-project-zh.png" });
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await page.getByRole("button", { name: longName, exact: true }).click({ button: "right" });
  const menu = page.getByRole("menu");
  await expect(menu.getByRole("menuitem", { name: "删除", exact: true })).toBeVisible();
  const geometry = await menu.evaluate((node) => ({ width: node.clientWidth, scroll: node.scrollWidth,
    labelWidth: node.querySelector("h3").clientWidth, labelScroll: node.querySelector("h3").scrollWidth }));
  expect(geometry.scroll).toBeLessThanOrEqual(geometry.width);
  expect(geometry.labelScroll).toBeGreaterThan(geometry.labelWidth);
  await expect(menu.getByRole("heading")).toHaveAttribute("title", longName);
  await page.screenshot({ path: "../../.artifacts/resource-context-menus-2026-10-24/project-menu-zh-long.png" });
});
