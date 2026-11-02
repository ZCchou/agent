import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

async function setup(page) {
  const { project, work, now } = await installTauriMock(page);
  const second = { ...project, id: "project-2", name: "Project Beta" };
  const chat = { ...work, id: "chat-1", title: "Ordinary chat", kind: "standard_chat", project_id: null,
    category: "casual", resolved_category: "casual", model_selection_mode: "fixed",
    provider_profile_id: "builtin-demo", model_id: "demo" };
  await page.addInitScript(({ project, second, work, chat, now }) => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    const projects = [project, second];
    const conversations = [work, chat];
    window.__calls = []; window.__errors = [];
    window.addEventListener("unhandledrejection", e => window.__errors.push(String(e.reason)));
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      window.__calls.push({ command, args });
      if (command === "list_projects") return structuredClone(projects);
      if (command === "list_conversations") return structuredClone(conversations);
      if (command === "get_conversation") return structuredClone(conversations.find(c => c.id === args.conversationId));
      if (command === "get_conversation_model_run") return { run: null };
      if (command === "get_project_grant") return { project_id: args.projectId, directory: window.__directory ?? "C:/old", active: true, root_generation: 1 };
      if (command === "choose_project_directory") {
        if (window.__directoryFailure) throw new Error("directory failed");
        return null;
      }
      if (command === "get_work_permission") return { mode: "human" };
      if (command === "list_project_knowledge") return { knowledge: [] };
      if (command === "get_project_model_scope") {
        if (window.__delayScope === args.projectId) return new Promise(resolve => { window.__finishScope = () => resolve({ project_id: args.projectId, policy: "restricted", models: [{ provider_profile_id: "builtin-demo", model_id: "third" }] }); });
        return { project_id: args.projectId, policy: "restricted", models: ["demo", "second", "third"].map(model_id => ({ provider_profile_id: "builtin-demo", model_id })) };
      }
      if (command === "get_app_settings") {
        const settings = structuredClone(await invoke(command, args));
        const provider = settings.models.providers[0];
        provider.models = ["demo", "second", "third"].map(id => ({ ...provider.models[0], id, tool_mode: "native" }));
        return settings;
      }
      if (command === "create_project") {
        const created = { ...project, id: `new-${projects.length}`, name: args.name, category: args.category };
        projects.push(created);
        if (window.__delayCreate) await new Promise(resolve => { window.__finishCreate = resolve; });
        return created;
      }
      if (command === "derive_work") {
        if (window.__deriveFailure) throw new Error("derive failed");
        if (window.__delayDerive) return new Promise(resolve => { window.__finishDerive = () => resolve(work); });
        return work;
      }
      if (command === "update_project_model_scope") {
        if (window.__delayModelWrite) await new Promise(resolve => { window.__finishModelWrite = resolve; });
        return { project_id: args.projectId, policy: args.policy, models: args.models };
      }
      if (command === "create_chat_group") throw new Error("group create failed");
      if (command === "update_conversation_routing") {
        const c = conversations.find(c => c.id === args.request.conversation_id);
        c.model_id = args.request.model_id; c.provider_profile_id = args.request.provider_profile_id;
        return { conversation: structuredClone(c) };
      }
      if (command === "connect_model_provider") return null;
      if (command === "start_model_run") throw new Error("synthetic stop after preflight");
      return invoke(command, args);
    };
  }, { project, second, work, chat, now });
  await page.goto("http://127.0.0.1:5173");
  await expect(page.getByRole("button", { name: "Implementation work", exact: true })).toBeVisible();
}
const count = (page, command) => page.evaluate(command => window.__calls.filter(c => c.command === command).length, command);
async function openProject(page, name = "Project Alpha") {
  await page.getByRole("button", { name, exact: true }).click();
  await expect(page.getByRole("heading", { name, level: 2 })).toBeVisible();
}

test("cancel directory keeps existing grant and work permission", async ({ page }) => {
  await setup(page);
  await page.getByRole("button", { name: "Implementation work", exact: true }).click();
  await expect(page.locator(".work-controls")).toContainText("C:/old");
  await page.getByRole("button", { name: "Change directory", exact: true }).click();
  await expect(page.locator(".work-controls")).toContainText("C:/old");
  await expect(page.locator('.work-controls select').first()).toBeEnabled();
});

test("ordinary chat owns category after a project work draft", async ({ page }) => {
  await setup(page);
  await openProject(page);
  await page.getByRole("button", { name: "New Work", exact: true }).click();
  await page.getByRole("button", { name: "Ordinary chat", exact: true }).click();
  await expect(page.locator(".top-bar select").first()).toBeEnabled();
  await expect(page.locator(".conversation-breadcrumb")).not.toContainText("Project Alpha");
});

test("returning to retained chat refreshes grant while preserving typed draft", async ({ page }) => {
  await setup(page);
  await page.getByRole("button", { name: "Implementation work", exact: true }).click();
  await page.getByPlaceholder("Ask agent...").fill("Keep this draft");
  await openProject(page);
  await page.evaluate(() => { window.__directory = "C:/new"; });
  await page.getByRole("button", { name: "Implementation work", exact: true }).click();
  await expect(page.locator(".work-controls")).toContainText("C:/new");
  await expect(page.getByPlaceholder("Ask agent...")).toHaveValue("Keep this draft");
});

test("top bar model choice is used by next send preflight", async ({ page }) => {
  await setup(page);
  await page.getByRole("button", { name: "Ordinary chat", exact: true }).click();
  await page.locator(".top-bar select").last().selectOption("builtin-demo::second");
  await expect.poll(() => count(page, "update_conversation_routing")).toBe(1);
  await page.getByPlaceholder("Ask agent...").fill("Use second");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__calls.find(c => c.command === "start_model_run")?.args.request.model_id)).toBe("second");
});

test("project creation retries directory failure without creating another project", async ({ page }) => {
  await setup(page);
  await page.evaluate(() => { window.__directoryFailure = true; });
  await page.getByTitle("New Project", { exact: true }).click();
  await page.getByRole("dialog").getByRole("textbox").fill("Created once");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.locator(".form-error")).toBeVisible();
  await page.evaluate(() => { window.__directoryFailure = false; });
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Created once", level: 2 })).toBeVisible();
  expect(await count(page, "create_project")).toBe(1);
});

test("derive retries reuse created project and admit only one pending submit", async ({ page }) => {
  await setup(page);
  await page.getByRole("button", { name: "Ordinary chat", exact: true }).click();
  await page.evaluate(() => { window.__deriveFailure = true; });
  await page.locator(".chat-toolbar").getByRole("button").filter({ hasText: /work/i }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("textbox").fill("Derived project");
  await dialog.getByRole("button", { name: "Create work", exact: true }).click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await page.evaluate(() => { window.__deriveFailure = false; window.__delayDerive = true; });
  await dialog.getByRole("button", { name: "Create work", exact: true }).click();
  await expect.poll(() => count(page, "derive_work")).toBe(2);
  await dialog.locator("form").evaluate(form => { form.requestSubmit(); form.requestSubmit(); });
  expect(await count(page, "create_project")).toBe(1);
  expect(await count(page, "derive_work")).toBe(2);
});

test("project model edits serialize and preserve both checkbox changes", async ({ page }) => {
  await setup(page);
  await openProject(page);
  const checks = page.locator(".project-model-scope input");
  await expect(checks).toHaveCount(3);
  await page.evaluate(() => { window.__delayModelWrite = true; });
  await checks.nth(0).click();
  await checks.nth(1).click();
  await page.evaluate(() => { window.__delayModelWrite = false; window.__finishModelWrite(); });
  await expect.poll(() => count(page, "update_project_model_scope")).toBe(2);
  await expect(checks.nth(0)).not.toBeChecked();
  await expect(checks.nth(1)).not.toBeChecked();
  expect(await page.evaluate(() => window.__calls.filter(c => c.command === "update_project_model_scope").at(-1).args.models.map(m => m.model_id))).toEqual(["third"]);
});

test("project A delayed model scope cannot replace project B", async ({ page }) => {
  await setup(page);
  await page.evaluate(() => { window.__delayScope = "project-1"; });
  await openProject(page);
  await expect.poll(() => page.evaluate(() => typeof window.__finishScope)).toBe("function");
  await openProject(page, "Project Beta");
  await expect(page.locator(".project-model-scope input:checked")).toHaveCount(3);
  await page.evaluate(() => window.__finishScope());
  await expect(page.locator(".project-model-scope input:checked")).toHaveCount(3);
});

test("sidebar group failure is visible and does not escape as rejection", async ({ page }) => {
  await setup(page);
  page.once("dialog", d => d.accept("Failed group"));
  await page.getByTitle("New chat group", { exact: true }).click();
  await expect(page.locator(".sidebar-resource-tree").getByRole("alert")).toBeVisible();
  expect(await page.evaluate(() => window.__errors)).toEqual([]);
});

test("project create synchronously admits one submit", async ({ page }) => {
  await setup(page);
  await page.evaluate(() => { window.__delayCreate = true; });
  await page.getByTitle("New Project", { exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("textbox").fill("Only once");
  await dialog.locator("form").evaluate(form => { form.requestSubmit(); form.requestSubmit(); });
  await expect.poll(() => count(page, "create_project")).toBe(1);
  await expect(dialog.getByRole("button", { name: "Cancel", exact: true })).toBeDisabled();
  await page.evaluate(() => window.__finishCreate());
  await expect(page.getByRole("heading", { name: "Only once", level: 2 })).toBeVisible();
});

test("work permission failure preserves the prior mode and can be retried", async ({ page }) => {
  await setup(page);
  await page.addInitScript(() => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "set_work_permission") {
        if (!window.__retryPermission) throw new Error("permission save failed");
        return { mode: args.mode };
      }
      return invoke(command, args);
    };
  });
  await page.reload();
  await page.getByRole("button", { name: "Implementation work", exact: true }).click();
  const select = page.locator(".work-controls select").first();
  await expect(select).toHaveValue("human");
  await select.selectOption("open");
  await expect(page.locator(".work-controls").getByRole("alert")).toBeVisible();
  await expect(select).toHaveValue("human");
  await page.evaluate(() => { window.__retryPermission = true; });
  await select.selectOption("open");
  await expect(select).toHaveValue("open");
  expect(await page.evaluate(() => window.__errors)).toEqual([]);
});

test("top bar rejected category change reports failure and keeps its value", async ({ page }) => {
  await setup(page);
  await page.addInitScript(() => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "update_conversation") throw new Error("category save failed");
      return invoke(command, args);
    };
  });
  await page.reload();
  await page.getByRole("button", { name: "Ordinary chat", exact: true }).click();
  const category = page.locator(".top-bar select").first();
  await category.selectOption("research");
  await expect(page.locator(".top-bar").getByRole("alert")).toBeVisible();
  await expect(category).toHaveValue("casual");
  expect(await page.evaluate(() => window.__errors)).toEqual([]);
});
