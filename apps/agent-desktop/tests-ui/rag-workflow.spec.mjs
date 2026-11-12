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
        case "library.overview": return {libraries:[{...library,document_count:2,ready_count:2,project_count:binding?1:0,processing_count:0}]};
        case "document.list": return {documents:library.documents.map(d=>({...d,adopted:true})),total:library.documents.length};
        case "document.history": return {versions:library.documents};
        case "ocr.model.list": return {models:[],runtime:{available:true}};
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
  await expect(page.getByRole("heading", { name: "资料库", level: 2 })).toBeVisible();
}

test("document preview keeps its passages readable in a desktop-sized window", async ({ page }) => {
  await openKnowledge(page);
  await page.setViewportSize({ width: 1002, height: 723 });
  await page.getByRole("button", {name:"打开资料库 Airworthiness library"}).click();
  await page.getByRole("button", {name:"预览",exact:true}).first().click();
  const passages=page.locator(".rag-reader-text");
  await expect(passages.locator("article")).toHaveCount(200);
  await expect.poll(()=>passages.evaluate(el=>el.clientHeight)).toBeGreaterThan(120);
  await page.getByRole("button",{name:"返回资料库",exact:true}).click();
});

test("auxiliary model import uses a native path grant and can be probed", async ({ page }) => {
  await openKnowledge(page);
  await page.getByRole("navigation").getByRole("button",{name:"Models",exact:true}).click();
  await page.getByRole("tab", { name: "检索模型",exact:true }).click();
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

test("manual version selection is available only in advanced library settings", async ({page}) => {
  await openKnowledge(page);
  await page.getByRole("button",{name:"打开资料库 Airworthiness library"}).click();
  await expect(page.getByRole("button",{name:"Build retrieval index"})).toHaveCount(0);
  await page.getByRole("tab",{name:"设置",exact:true}).click();
  await page.getByText("高级维护",{exact:true}).click();
  const build=page.getByRole("button",{name:"Build retrieval index"});
  await expect(build).toBeDisabled();
  const standard=page.getByRole("group",{name:"Airworthiness standard",exact:true});
  await standard.getByRole("radio").nth(1).check();
  await expect(build).toBeEnabled();
  await build.click();
  await expect.poll(()=>page.evaluate(()=>window.__ragCalls.some(x=>x.args.action==='index.build' && x.args.params.version_ids.includes('standard-r2')))).toBe(true);
});
