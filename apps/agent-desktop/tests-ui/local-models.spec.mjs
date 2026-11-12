import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

async function setup(page, declaredMaximum = 32768) {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installTauriMock(page);
  await page.addInitScript(declaredMaximum => {
    const original = window.__TAURI_INTERNALS__.invoke;
    const id = "a".repeat(64);
    const record = { id, provider_profile_id: `managed:${id}`, name: "Fixture GGUF", architecture: "llama", quantization: "Q4_K", context_length: 8192, max_context_length: 32768, size_bytes: 1048576, digest: id, device: "cpu", threads: 4, gpu_layers: -1, source: "test fixture", license: "MIT", status: "imported", is_default: false, source_type: "managed_local", tool_mode: "text_only", files: [], capabilities: {}, created_at: "2026-11-07" };
    const models = []; const jobs = [];
    record.declared_context_length = declaredMaximum;
    let selected = false; let runtime = { state: "stopped" };
    window.__localCalls = []; window.__localRoutingCalls = []; let routing = null;
    window.__restrictLocalModel = false;
    window.__localLoadDelay = 100; window.__localSubmitted = [];
    const routedConversation = async () => ({ ...await original("get_conversation"),
      model_selection_mode: routing.mode, provider_profile_id: routing.provider_profile_id, model_id: routing.model_id,
      coordinator_provider_profile_id: routing.provider_profile_id, coordinator_model_id: routing.model_id });
    window.__TAURI_INTERNALS__.invoke = async (command, args = {}) => {
      if (command === "local_model_select_path") { selected = true; return { grant_id: "native-mock-grant", path: "C:/fixtures/main.gguf" }; }
      if (command === "local_model_revoke_path") { selected = false; return; }
      if (command === "get_app_settings") {
        const settings = structuredClone(await original(command, args));
        if (models.length) {
          settings.models.providers.push({ id: record.provider_profile_id, name: record.name, kind: "openai_compatible", preset_id: "custom", entered_base_url: "", resolved_api_base_url: null, endpoint_mode: "exact", enabled: true, credential_ref: null, models: [{ id, source: "discovered", status: record.status === "loaded" ? "available" : "unavailable", enabled: true, allow_automatic_routing: true, tool_mode: "text_only", capabilities: { text: "yes", vision: "no", reasoning: "unknown", tools: "no", context_length: 8192, context_length_source: "discovered" } }] });
          if (record.is_default) { settings.models.default_provider_profile_id = record.provider_profile_id; settings.models.default_model_id = id; }
        }
        return structuredClone(settings);
      }
      if (command === "update_conversation_routing") {
        window.__localRoutingCalls.push(structuredClone(args.request)); routing = args.request;
        return { conversation: await routedConversation() };
      }
      if (command === "list_conversations" && routing) return [await routedConversation()];
      if (command === "get_conversation" && routing) return routedConversation();
      if (command === "get_project_model_scope" && window.__restrictLocalModel) return { project_id: "project-1", policy: "restricted", models: [] };
      if (command === "connect_model_provider") return null;
      if (command === "start_model_run") {
        window.__localSubmitted.push(structuredClone(args.request));
        const conversation = await routedConversation();
        const user = { id: "local-user", conversation_id: conversation.id, role: "user", content: args.request.content, created_at: "2026-11-08" };
        const assistant = { id: "local-assistant", conversation_id: conversation.id, role: "assistant", content: "Local model response", created_at: "2026-11-08" };
        window.__localRun = { id: "local-run", conversation_id: conversation.id, user_message_id: user.id,
          status: "completed", provider_profile_id: record.provider_profile_id, model_id: id, assistant_message: assistant };
        return { status: "accepted", conversation, user_message: user, run: window.__localRun };
      }
      if (command === "get_model_run") return { run: window.__localRun };
      if (command === "update_default_model") { record.is_default = true; return { settings: await window.__TAURI_INTERNALS__.invoke("get_app_settings"), core_sync: "applied", warning_code: null }; }
      if (command !== "local_model_request") return original(command, args);
      window.__localCalls.push(structuredClone(args));
      switch (args.action) {
        case "model.list": return structuredClone({ models, root: "C:/models", storage_migration: null });
        case "runtime.status": return structuredClone(runtime);
        case "runtime.capabilities": return { devices: ["cpu"], runtimes: [{ backend: "cpu", integrity: args.params.verify ? "verified" : "unchecked" }] };
        case "job.list": return structuredClone({ jobs });
        case "model.inspect": if (!selected || args.params.grant_id !== "native-mock-grant") throw Error("Native grant required"); return { model: structuredClone(record) };
        case "model.import": if (!selected) throw Error("Native grant required"); selected = false; models.push(record); jobs.push({ id: "import", kind: "import", status: "running", progress: 0.5 }); return { job: structuredClone(jobs[0]) };
        case "model.configure": Object.assign(record, args.params); return { model: structuredClone(record) };
        case "model.configure_load":
          Object.assign(record, args.params);
          return window.__TAURI_INTERNALS__.invoke("local_model_request", { action:"runtime.load", params:{model_id:id} });
        case "runtime.prepare": {
          const preparation_id = args.params.preparation_id ?? "fixture-preparation";
          if (record.status === "loaded") return { preparation_id, state: "ready" };
          if (args.params.preparation_id) {
            const job = jobs.filter(job => job.kind === "load").at(-1);
            return { preparation_id, state: job?.status === "failed" || job?.status === "cancelled" ? "failed" : "loading", job: structuredClone(job) };
          }
          const { job } = await window.__TAURI_INTERNALS__.invoke("local_model_request", { action: "runtime.load", params: { model_id: id } });
          return { preparation_id, state: "loading", job };
        }
        case "runtime.release": {
          const job = jobs.filter(job => job.kind === "load").at(-1);
          if (job && ["queued", "running"].includes(job.status)) {
            job.status = "cancelled"; record.status = "imported"; runtime = { state: "stopped" };
          }
          return { released: true };
        }
        case "runtime.load":
          {
            const job = { id: `load-${jobs.length}`, kind: "load", model_id: id, status: "queued", progress: 0, error: null, created_at: "2026-11-08", updated_at: "2026-11-08" };
            jobs.push(job); record.status = "loading";
            const device = record.device;
            window.setTimeout(() => {
              if (job.status === "cancelled") return;
              if (device === "cuda") {
                const error = { code: "device_unavailable", message: "CUDA device unavailable" };
                record.status = "error"; record.error = error;
                job.status = "failed"; job.error = error;
                runtime = { state: "failed", model_id: id, backend: null, error };
              } else {
                record.status = "loaded"; record.error = null;
                job.status = "completed"; job.progress = 1;
                jobs[0].status = "completed"; jobs[0].progress = 1;
                runtime = { state: "loaded", model_id: id, backend: device === "auto" ? "cuda" : "cpu", gpu_layers_used: device === "auto" ? 2 : 0, context_length: 8192, error: null };
              }
            }, window.__localLoadDelay);
            return { job: structuredClone(job) };
          }
        case "runtime.unload": record.status = "imported"; runtime = { state: "stopped" }; return runtime;
        case "job.cancel": jobs[0].status = "cancelled"; return { job: structuredClone(jobs[0]) };
        default: throw Error(`Unsupported fixture action ${args.action}`);
      }
    };
  }, declaredMaximum);
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "System features" }).click();
  await page.getByRole("button", { name: "Models", exact: true }).click();
  await expect(page.getByRole("heading", { name: "本地模型", exact: true })).toBeVisible();
}
async function importModel(page) {
  await page.getByRole("button", { name: "选择 GGUF 文件" }).click();
  await expect(page.getByText("C:/fixtures/main.gguf", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "导入所选模型" }).click();
  await expect(page.getByRole("heading", { name: "Fixture GGUF", exact: true })).toBeVisible();
}
test("maximum-context shortcut fills declared maximum and saves through the normal form", async ({ page }) => {
  await setup(page); await importModel(page);
  const input = page.getByRole('spinbutton', { name: '上下文', exact: true });
  await expect(input).toHaveValue('8192');
  await page.getByRole('button', { name: '使用模型声明的最大上下文', exact: true }).click();
  await expect(input).toHaveValue('32768');
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  expect(await page.evaluate(() => window.__localCalls.find(call => call.action === 'model.configure').params.context_length)).toBe(32768);
});

test("maximum-context shortcut is unavailable when declaration is missing", async ({ page }) => {
  await setup(page, null); await importModel(page);
  await expect(page.getByRole('button', { name: '使用模型声明的最大上下文', exact: true })).toBeDisabled();
  await expect(page.getByText('模型文件未声明最大上下文，无法使用此操作。', { exact: true })).toBeVisible();
  await expect(page.getByRole('spinbutton', { name: '上下文', exact: true })).toHaveValue('8192');
});

test("mocked native picker imports, loads, defaults and exposes stable chat selection", async ({ page }) => {
  await setup(page); await importModel(page);
  await page.getByRole("button", { name: "加载模型", exact: true }).click();
  await expect(page.getByText(/实际后端 cpu/)).toBeVisible();
  await page.getByRole("button", { name: "设为默认", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Fixture GGUF · 默认模型" })).toBeVisible();
  await page.getByRole("button", { name: "Implementation work", exact: true }).click();
  await expect(page.locator(`.top-bar option[value="managed:${"a".repeat(64)}::${"a".repeat(64)}"]`)).toHaveCount(1);
  await page.locator(".top-bar select").last().selectOption(`managed:${"a".repeat(64)}::${"a".repeat(64)}`);
  await expect.poll(() => page.evaluate(() => window.__localRoutingCalls.length)).toBe(1);
  expect(await page.evaluate(() => window.__localRoutingCalls[0].provider_profile_id)).toBe(`managed:${"a".repeat(64)}`);
  expect(await page.evaluate(() => window.__localCalls.find(call => call.action === "model.import").params.grant_id)).toBe("native-mock-grant");
});
test("mocked import task can be cancelled", async ({ page }) => {
  await setup(page); await importModel(page);
  await page.getByRole("button", { name: "取消任务", exact: true }).click();
  await expect(page.getByText("import · cancelled", { exact: true })).toBeVisible();
});

for (const mode of ["fixed", "automatic"]) {
  test(`${mode} chat automatically reloads an idle-released managed model before sending`, async ({ page }) => {
    await setup(page); await importModel(page);
    await page.getByRole("button", { name: "加载模型", exact: true }).click();
    await expect(page.getByText(/实际后端 cpu/)).toBeVisible();
    await page.evaluate(mode => window.__TAURI_INTERNALS__.invoke("update_conversation_routing", { request: {
      conversation_id: "work-1", mode, provider_profile_id: `managed:${"a".repeat(64)}`, model_id: "a".repeat(64),
    } }), mode);
    await page.getByRole("button", { name: "Implementation work", exact: true }).click();
    // Model disappears while the chat remains open, as with idle release.
    await page.evaluate(() => window.__TAURI_INTERNALS__.invoke("local_model_request", { action: "runtime.unload", params: {} }));
    const editor = page.getByRole("textbox", { name: "Ask agent..." });
    await editor.fill("Continue with local evidence");
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(page.getByText("Local model response", { exact: true })).toBeVisible();
    expect(await page.evaluate(() => window.__localRoutingCalls.length)).toBe(1);
    expect(await page.evaluate(() => window.__localCalls.some(call => call.action === "runtime.prepare"))).toBe(true);
    expect(await page.evaluate(() => window.__localCalls.some(call => call.action === "runtime.release"))).toBe(true);
  });
}

test("an out-of-scope cold managed model still requires a compatible selection", async ({ page }) => {
  await setup(page); await importModel(page);
  await page.evaluate(async () => {
    window.__restrictLocalModel = true;
    await window.__TAURI_INTERNALS__.invoke("update_conversation_routing", { request: {
      conversation_id: "work-1", mode: "fixed", provider_profile_id: `managed:${"a".repeat(64)}`, model_id: "a".repeat(64),
    } });
  });
  await page.getByRole("button", { name: "Implementation work", exact: true }).click();
  await page.getByRole("textbox", { name: "Ask agent..." }).fill("Restricted evidence");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByText(/The current model is not allowed for this conversation or project/)).toBeVisible();
  await expect(page.getByText(/The selected local model is not loaded/)).toHaveCount(0);
});

test("top selector loads an imported local model before binding the conversation", async ({ page }) => {
  await setup(page); await importModel(page);
  await page.getByRole("button", { name: "Implementation work", exact: true }).click();
  const value = `managed:${"a".repeat(64)}::${"a".repeat(64)}`;
  await page.locator(".top-bar select").last().selectOption(value);
  await expect(page.locator(".top-bar select").last()).toHaveValue(value);
  await expect(page.locator(".top-bar select").last()).toBeEnabled();
  await page.getByRole("textbox", { name: "Ask agent..." }).fill("Use selected local model");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByText("Local model response", { exact: true })).toBeVisible();
});

test("a new chat can select and load its local default without opening model settings", async ({ page }) => {
  await setup(page); await importModel(page);
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  const value = `managed:${"a".repeat(64)}::${"a".repeat(64)}`;
  await page.locator(".top-bar select").last().selectOption(value);
  await expect(page.locator(".top-bar select").last()).toHaveValue(value);
  await expect(page.locator(".top-bar select").last()).toBeEnabled();
  await page.getByRole("button", { name: "Models", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Fixture GGUF · 默认模型" })).toBeVisible();
  await expect(page.getByText(/实际后端 cpu/)).toBeVisible();
});

test("model settings can select an unloaded imported model as the default", async ({ page }) => {
  await setup(page); await importModel(page);
  await page.locator(".models-default-card select").first().selectOption(`managed:${"a".repeat(64)}`);
  await page.locator(".models-default-card select").last().selectOption("a".repeat(64));
  await expect(page.getByRole("heading", { name: "Fixture GGUF · 默认模型" })).toBeVisible();
});

test("a failed local load keeps the previous conversation model", async ({ page }) => {
  await setup(page); await importModel(page);
  await page.locator('.local-model-config select[name="device"]').selectOption("cuda");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await page.getByRole("button", { name: "Implementation work", exact: true }).click();
  await page.locator(".top-bar select").last().selectOption(`managed:${"a".repeat(64)}::${"a".repeat(64)}`);
  await expect(page.locator(".top-bar [role=alert]")).toBeVisible();
  await expect(page.locator(".top-bar select").last()).toBeEnabled();
  await page.getByRole("button", { name: "Models", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "CUDA device unavailable" })).toHaveCount(3);
  expect(await page.evaluate(() => window.__localRoutingCalls)).toEqual([]);
});

test("leaving a conversation while its model loads does not change the new chat default", async ({ page }) => {
  await setup(page); await importModel(page);
  await page.getByRole("button", { name: "Implementation work", exact: true }).click();
  await page.locator(".top-bar select").last().selectOption(`managed:${"a".repeat(64)}::${"a".repeat(64)}`);
  await expect(page.locator(".top-bar select").last()).toBeDisabled();
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await expect(page.locator(".top-bar select").last()).toBeEnabled();
  await page.getByRole("button", { name: "Models", exact: true }).click();
  await expect(page.getByText(/实际后端 cpu/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Fixture GGUF · 默认模型" })).toHaveCount(0);
  expect(await page.evaluate(() => window.__localRoutingCalls)).toEqual([]);
});
test("queued GPU failure survives refresh, successful retry and reopening with its historical job", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  await setup(page); await importModel(page);
  await page.locator('.local-model-config select[name="device"]').selectOption("cuda");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await page.getByRole("button", { name: "加载模型", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "CUDA device unavailable" })).toHaveCount(3);
  await expect(page.getByRole("alert").filter({ hasText: "device_unavailable" })).toHaveCount(3);
  await expect(page.locator('.local-model-config select[name="device"]')).toHaveValue("cuda");
  await page.locator('.local-model-config select[name="device"]').selectOption("cpu");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await page.getByRole("button", { name: "加载模型", exact: true }).click();
  await expect(page.getByText(/实际后端 cpu/)).toBeVisible();
  await expect(page.getByText("load · completed", { exact: true })).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "CUDA device unavailable" })).toHaveCount(1);
  await page.getByRole("button", { name: "Implementation work", exact: true }).click();
  await page.getByRole("button", { name: "Models", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Fixture GGUF", exact: true })).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "device_unavailable" })).toHaveCount(1);
  await expect(page.getByText("load · completed", { exact: true })).toBeVisible();
  expect(pageErrors).toEqual([]);
});


test("cancelling send during automatic loading preserves the question and never submits it", async ({ page }) => {
  await setup(page); await importModel(page);
  await page.evaluate(async () => {
    window.__localLoadDelay = 4000;
    await window.__TAURI_INTERNALS__.invoke("update_conversation_routing", { request: {
      conversation_id: "work-1", mode: "fixed", provider_profile_id: `managed:${"a".repeat(64)}`, model_id: "a".repeat(64),
    } });
  });
  await page.getByRole("button", { name: "Implementation work", exact: true }).click();
  const editor = page.getByRole("textbox", { name: "Ask agent..." });
  await editor.fill("Keep this question after cancellation");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__localCalls.some(call => call.action === "runtime.prepare"))).toBe(true);
  await page.getByRole("button", { name: "Stop generating", exact: true }).click();
  await expect(editor).toHaveValue("Keep this question after cancellation");
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeVisible();
  expect(await page.evaluate(() => window.__localSubmitted.length)).toBe(0);
  expect(await page.evaluate(() => window.__localCalls.filter(call => call.action === "runtime.load").length)).toBe(1);
});


test("a loaded model exposes unload beside configuration and can save and reload another device", async ({ page }) => {
  await setup(page); await importModel(page);
  await page.getByRole("button", { name:"加载模型", exact:true }).click();
  await expect(page.getByText(/实际后端 cpu/)).toBeVisible();
  const card=page.locator(".local-model-record");
  await expect(card.getByRole("button", {name:"卸载模型",exact:true})).toBeEnabled();
  await expect(card.getByRole("button", {name:"重新加载模型",exact:true})).toBeEnabled();
  await card.locator('select[name="device"]').selectOption("auto");
  await card.getByRole("button", {name:"保存配置并重新加载",exact:true}).click();
  await expect(page.getByText(/实际后端 cuda/)).toBeVisible();
  await expect(card.locator('select[name="device"]')).toHaveValue("auto");
  expect(await page.evaluate(() => window.__localCalls.some(call=>call.action==="model.configure_load" && call.params.device==="auto"))).toBe(true);
});
