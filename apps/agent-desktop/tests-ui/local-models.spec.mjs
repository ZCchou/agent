import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

async function setup(page) {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installTauriMock(page);
  await page.addInitScript(() => {
    const original = window.__TAURI_INTERNALS__.invoke;
    const id = "a".repeat(64);
    const record = { id, provider_profile_id: `managed:${id}`, name: "Fixture GGUF", architecture: "llama", quantization: "Q4_K", context_length: 8192, max_context_length: 32768, size_bytes: 1048576, digest: id, device: "cpu", threads: 4, gpu_layers: -1, source: "test fixture", license: "MIT", status: "imported", is_default: false, source_type: "managed_local", tool_mode: "text_only", files: [], capabilities: {}, created_at: "2026-11-07" };
    const models = []; const jobs = [];
    let selected = false; let runtime = { state: "stopped" };
    window.__localCalls = []; window.__localRoutingCalls = []; let routing = null;
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
        return { conversation: { ...await original("get_conversation"), model_selection_mode: routing.mode, provider_profile_id: routing.provider_profile_id, model_id: routing.model_id } };
      }
      if (command === "list_conversations" && routing) return (await original(command, args)).map(conversation => ({ ...conversation, model_selection_mode: routing.mode, provider_profile_id: routing.provider_profile_id, model_id: routing.model_id }));
      if (command === "get_conversation" && routing) return { ...await original(command, args), model_selection_mode: routing.mode, provider_profile_id: routing.provider_profile_id, model_id: routing.model_id };
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
        case "runtime.load":
          {
            const job = { id: `load-${jobs.length}`, kind: "load", model_id: id, status: "queued", progress: 0, error: null, created_at: "2026-11-08", updated_at: "2026-11-08" };
            jobs.push(job); record.status = "loading";
            const device = record.device;
            window.setTimeout(() => {
              if (device === "cuda") {
                const error = { code: "device_unavailable", message: "CUDA device unavailable" };
                record.status = "error"; record.error = error;
                job.status = "failed"; job.error = error;
                runtime = { state: "failed", model_id: id, backend: null, error };
              } else {
                record.status = "loaded"; record.error = null;
                job.status = "completed"; job.progress = 1;
                jobs[0].status = "completed"; jobs[0].progress = 1;
                runtime = { state: "loaded", model_id: id, backend: "cpu", gpu_layers_used: 0, context_length: 8192, error: null };
              }
            }, 100);
            return { job: structuredClone(job) };
          }
        case "runtime.unload": record.status = "imported"; runtime = { state: "stopped" }; return runtime;
        case "job.cancel": jobs[0].status = "cancelled"; return { job: structuredClone(jobs[0]) };
        default: throw Error(`Unsupported fixture action ${args.action}`);
      }
    };
  });
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
