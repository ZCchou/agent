import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

async function setup(page) {
  await installTauriMock(page);
  await page.addInitScript(() => {
    const original = window.__TAURI_INTERNALS__.invoke;
    let skills = [];
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "list_skills") return skills;
      if (command === "import_skill_directory") {
        const skill = { id: "lesson", name: "lesson", description: "Teach with examples", path: "C:\\fixtures\\lesson", enabled: true, implicit: true, error: null };
        skills = [skill]; return skill;
      }
      if (command === "set_skill_enabled") { skills[0].enabled = args.enabled; return skills; }
      if (command === "remove_skill") { skills = []; return []; }
      if (command === "inspect_skill") return { ...skills[0], content: skills[0].content ?? "Explain using a small worked example.", revision: "a".repeat(64) };
      if (command === "save_skill") {
        const skill = { id: args.draft.name, ...args.draft, path: "C:\\fixtures\\" + args.draft.name, enabled: true, implicit: true, error: null, revision: "b".repeat(64) };
        skills = [skill]; return skill;
      }
      return original(command, args);
    };
  });
  await page.goto("http://127.0.0.1:5173");
}

test("skills can be imported, inspected, disabled, enabled and forgotten", async ({ page }) => {
  await setup(page);
  await page.getByRole("button", { name: "System features" }).click();
  await page.getByRole("navigation").getByRole("button", { name: "Skills", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Skills", level: 2 })).toBeVisible();
  await page.getByRole("button", { name: "Import skill folder" }).click();
  await expect(page.getByText("Teach with examples")).toBeVisible();
  await page.getByRole("button", { name: "View instructions" }).click();
  await expect(page.getByText("Explain using a small worked example.")).toBeVisible();
  await page.getByRole("checkbox", { name: "Enable lesson" }).uncheck();
  await expect(page.getByRole("checkbox", { name: "Enable lesson" })).not.toBeChecked();
  await page.getByRole("checkbox", { name: "Enable lesson" }).check();
  await page.getByRole("button", { name: "Remove registration" }).click();
  await expect(page.getByText("No skills imported yet.")).toBeVisible();
});

test("composer selects an explicit skill in the visible message", async ({ page }) => {
  await setup(page);
  await page.evaluate(() => window.__TAURI_INTERNALS__.invoke("import_skill_directory"));
  await page.getByRole("button", { name: "Choose a skill" }).click();
  await page.getByRole("button", { name: "lesson", exact: true }).click();
  await expect(page.locator(".chat-composer textarea")).toHaveValue("$lesson ");
  await expect(page.getByRole("button", { name: "Choose a skill" })).toBeVisible();
});

test("skill picker preserves a full draft", async ({ page }) => {
  await setup(page);
  await page.evaluate(() => window.__TAURI_INTERNALS__.invoke("import_skill_directory"));
  const value = "x".repeat(19992) + "END-KEEP";
  await page.locator(".chat-composer textarea").fill(value);
  await page.getByRole("button", { name: "Choose a skill" }).click();
  await page.getByRole("button", { name: "lesson", exact: true }).click();
  await expect(page.locator(".chat-composer textarea")).toHaveValue(value);
  await expect(page.getByRole("alert")).toContainText("space");
});

test("skills can be created and edited in the app", async ({ page }) => {
  test.setTimeout(40_000);
  await page.setViewportSize({ width: 1100, height: 850 });
  await setup(page);
  await page.getByRole("button", { name: "System features" }).click();
  await page.getByRole("navigation").getByRole("button", { name: "Skills", exact: true }).click();
  await page.getByRole("button", { name: "Create skill", exact: true }).click();
  await page.getByLabel("Skill name", { exact: true }).fill("my-lesson");
  await page.getByLabel("When to use", { exact: true }).fill("Teaching neurons");
  await page.getByLabel("Instructions (Markdown)", { exact: true }).fill("Start with a neuron.");
  await page.getByRole("button", { name: "Save skill", exact: true }).click();
  await expect(page.getByText("Start with a neuron.", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Edit skill", exact: true }).click();
  await expect(page.getByLabel("Skill name", { exact: true })).toBeDisabled();
  await page.screenshot({ path: "../../.artifacts/skills-2026-10-24/skills-editor.png", fullPage: true });
  await page.getByLabel("Instructions (Markdown)", { exact: true }).fill("Then explain gradients.");
  await page.getByRole("button", { name: "Save skill", exact: true }).click();
  await expect(page.getByText("Then explain gradients.", { exact: true })).toBeVisible();
});
