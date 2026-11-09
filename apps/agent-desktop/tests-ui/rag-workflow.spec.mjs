import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

async function openKnowledge(page) {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installTauriMock(page);
  await page.addInitScript(() => {
    const original = window.__TAURI_INTERNALS__.invoke;
    const grants = new Map();
    const models = [{ id: "ranking", name: "Local ranking model", role: "reranker", backend: "transformers", profile: "bge-reranker-v2-m3", device: "cpu", status: "ready", digest: "ranking-digest", dimension: null, size_bytes: 1024 }];
    const version = { id: "standard", title: "Airworthiness standard", status: "confirmed", warnings: [], chunk_count: 3 };
    const library = {
      id: "airworthiness", name: "Airworthiness library", embedding_model_id: "embedding", reranker_model_id: "ranking",
      active_index_id: "index-initial", generation: 1,
      documents: [
        { ...version, version_id: "standard-r1", revision: "R1" },
        { ...version, version_id: "standard-r2", revision: "R2" },
        { ...version, id: "appendix", title: "Supporting appendix", version_id: "appendix-r1", revision: "A1" },
      ],
    };
    let binding = null;
    const jobs = [];
    window.__ragCalls = [];
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "rag_select_paths") {
        window.__ragCalls.push({ command, args: structuredClone(args) });
        const selected = { grant_id: "selected-model-grant", paths: ["C:\\fixtures\\bge-m3"] };
        grants.set(selected.grant_id, selected.paths[0]);
        return selected;
      }
      if (command === "rag_revoke_paths") { grants.delete(args.grantId); return; }
      if (command !== "rag_request") return original(command, args);
      window.__ragCalls.push({ command, args: structuredClone(args) });
      const { action, params } = args;
      switch (action) {
        case "model.list": return structuredClone({ models, root: "C:\\local-models", runtime: { available: true, devices: ["cpu"] } });
        case "model.inspect":
        case "model.import": {
          if (grants.get(params.grant_id) !== params.path) throw new Error("A matching native selection grant is required");
          if (action === "model.inspect") return { valid: true, errors: [], role: "embedding", dimension: 1024 };
          grants.delete(params.grant_id);
          const imported = { id: "embedding", name: "Imported BGE model", role: "embedding", backend: "transformers", profile: params.profile, device: params.device, status: "imported", digest: "embedding-digest", dimension: null, size_bytes: 1048576 };
          models.push(imported);
          return structuredClone(imported);
        }
        case "model.probe": {
          const model = models.find((item) => item.id === params.model_id);
          model.status = "ready"; model.dimension = 1024;
          return structuredClone(model);
        }
        case "library.list": return structuredClone({ libraries: params.project_id ? binding ? [{ ...library, ...binding }] : [] : [library] });
        case "library.bind":
          binding = { project_id: params.project_id, enabled: params.enabled, index_id: params.enabled ? library.active_index_id : null };
          return { bound: params.enabled };
        case "index.build":
          library.active_index_id = "index-reviewed"; library.generation += 1;
          jobs.push({ id: "index-job", kind: "index.build", library_id: library.id, status: "completed", progress: 1 });
          return { job: structuredClone(jobs[0]) };
        case "job.list": return structuredClone({ jobs });
        case "document.preview": return {
          version: { ...version, id: params.version_id, revision: "R1", source: "Local PDF", document_number: "DO-160" },
          warnings: ["OCR is running. Image regions are not yet included."],
          chunks: Array.from({ length: 200 }, (_, index) => ({
            id: `chunk-${index}`, text: `Clause ${index + 1}. Environmental qualification requirements. `.repeat(12), locator: { page: index + 1 },
          })),
          total_chunks: 386,
        };
        default: throw new Error(`Unexpected RAG action: ${action}`);
      }
    };
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: "System features" }).click();
  await page.getByRole("navigation").getByRole("button", { name: "Local knowledge", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Local knowledge", level: 2 })).toBeVisible();
}

test("document preview keeps its passages readable in a desktop-sized window", async ({ page }) => {
  await openKnowledge(page);
  await page.setViewportSize({ width: 1002, height: 723 });
  await page.getByRole("button", { name: "Review and confirm" }).first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  const passages = dialog.locator(".rag-passages");
  await expect(passages.locator("article")).toHaveCount(200);
  await expect.poll(() => passages.evaluate((element) => element.clientHeight)).toBeGreaterThan(120);
  await passages.scrollIntoViewIfNeeded();
  await expect(passages.locator("article").first()).toBeVisible();
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await expect(dialog).toHaveCount(0);
});

test("auxiliary model import uses a native path grant and can be probed", async ({ page }) => {
  await openKnowledge(page);
  await page.getByRole("tab", { name: "Auxiliary models" }).click();
  await page.getByRole("combobox", { name: "Model type", exact: true }).selectOption("bge-m3");
  await page.getByRole("button", { name: "Choose model folder" }).click();
  await expect(page.getByText("C:\\fixtures\\bge-m3", { exact: true })).toBeVisible();
  await expect(page.getByText("Files checked. Confirm to import.")).toBeVisible();
  await page.getByRole("button", { name: "Import model", exact: true }).click();
  const modelCard = page.locator(".rag-card").filter({ has: page.getByRole("heading", { name: "Imported BGE model", exact: true }) });
  await expect(modelCard).toContainText("Imported");
  await modelCard.getByRole("button", { name: "Check availability" }).click();
  await expect(modelCard).toContainText("Status Ready");
  await expect(modelCard).toContainText("Vector dimensions 1024");
  const calls = await page.evaluate(() => window.__ragCalls.filter((call) => call.command === "rag_select_paths" || ["model.inspect", "model.import", "model.probe"].includes(call.args.action)));
  expect(calls).toEqual([
    { command: "rag_select_paths", args: { kind: "model", modelFile: false } },
    { command: "rag_request", args: { action: "model.inspect", params: { path: "C:\\fixtures\\bge-m3", grant_id: "selected-model-grant", profile: "bge-m3" } } },
    { command: "rag_request", args: { action: "model.import", params: { path: "C:\\fixtures\\bge-m3", grant_id: "selected-model-grant", profile: "bge-m3", device: "cpu" } } },
    { command: "rag_request", args: { action: "model.probe", params: { model_id: "embedding" } } },
  ]);
});

test("a project keeps its bound index until reviewed revisions are built and adopted", async ({ page }) => {
  await openKnowledge(page);
  await page.getByRole("combobox", { name: "Project", exact: true }).selectOption("project-1");
  const library = page.locator(".rag-card").filter({ has: page.getByRole("heading", { name: "Airworthiness library", exact: true }) });
  await library.getByRole("button", { name: "Enable for project" }).click();
  await expect(library.getByText("Index used by this project index-initial", { exact: true })).toBeVisible();
  const build = library.getByRole("button", { name: "Build retrieval index" });
  await expect(build).toBeDisabled();
  const standard = library.getByRole("group", { name: "Airworthiness standard", exact: true });
  const appendix = library.getByRole("group", { name: "Supporting appendix", exact: true });
  await expect(appendix.getByRole("radio", { name: "A1 · Supporting appendix" })).toBeChecked();
  await standard.getByRole("radio", { name: "R2 · Airworthiness standard" }).check();
  await appendix.getByRole("radio", { name: "Exclude this source" }).check();
  await expect(standard.getByRole("radio", { name: "R1 · Airworthiness standard" })).not.toBeChecked();
  await build.click();
  await expect(library.getByText("Library current index index-reviewed", { exact: true })).toBeVisible();
  await expect(library.getByText("Index used by this project index-initial", { exact: true })).toBeVisible();
  await library.getByRole("button", { name: "Use current index" }).click();
  await expect(library.getByText("Index used by this project index-reviewed", { exact: true })).toBeVisible();
  await expect(library.getByRole("button", { name: "Use current index" })).toBeDisabled();
  const calls = await page.evaluate(() => window.__ragCalls.filter((call) => ["library.bind", "index.build"].includes(call.args.action)).map((call) => call.args));
  expect(calls).toEqual([
    { action: "library.bind", params: { library_id: "airworthiness", project_id: "project-1", enabled: true } },
    { action: "index.build", params: { library_id: "airworthiness", version_ids: ["standard-r2"] } },
    { action: "library.bind", params: { library_id: "airworthiness", project_id: "project-1", enabled: true } },
  ]);
});
