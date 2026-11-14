import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";
async function setup(page, scenario = {}) {
  await installTauriMock(page);
  await page.addInitScript((scenario) => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    const library = {
      id: "lib",
      name: "适航资料",
      documents: [],
      embedding_model_id: "embed",
      reranker_model_id: "rank",
      active_index_id: "idx",
      generation: 1,
      document_count: 1,
      ready_count: 1,
      project_count: 0,
      processing_count: 0,
    };
    const doc = {
      id: "doc",
      version_id: "final",
      title: "DO-160F",
      original_name: "DO-160F.pdf",
      revision: "F",
      status: "confirmed",
      adopted: true,
      history_count: 2,
      chunk_count: 663,
      human_reviewed: 0,
      warnings: [],
      ocr_summary: { coverage: "complete", ocr_pages: 64, total_pages: 300 },
    };
    let bound = false;
    const task = {
      id: "batch",
      kind: "import.batch",
      library_id: "lib",
      status: "running",
      phase: "ocr",
      filename: "DO-160F.pdf",
      progress: 0,
      completed_units: 18,
      total_units: 64,
      created_at: new Date().toISOString(),
    };
    window.ragCalls = [];
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "rag_select_paths")
        return { grant_id: "file-grant", paths: ["C:/fixtures/DO-160F.pdf"] };
      if (command === "rag_revoke_paths") return;
      if (command !== "rag_request") return invoke(command, args);
      window.ragCalls.push(args);
      switch (args.action) {
        case "library.overview":
          return {
            libraries: [
              {
                ...library,
                ...(args.params.project_id && bound
                  ? { enabled: 1, index_id: "idx" }
                  : {}),
              },
            ],
          };
        case "library.list":
          return {
            libraries: bound
              ? [
                  {
                    ...library,
                    enabled: 1,
                    index_id: "idx",
                    project_id: "project-1",
                  },
                ]
              : [],
          };
        case "document.list":
          return { documents: [doc], total: 1 };
        case "job.list":
          return { jobs: scenario.task ? [task] : [] };
        case "document.inspect":
          return {
            files: [
              {
                path: "C:/fixtures/DO-160F.pdf",
                name: "DO-160F.pdf",
                size_bytes: 100,
                sha256: "hash",
                duplicate: scenario.duplicate
                  ? { id: "final", document_id: "doc", title: "DO-160F" }
                  : null,
                conflict_document_id: null,
              },
            ],
          };
        case "document.metadata":
          return { version_id: "reviewed" };
        case "document.import":
          return { job: task, jobs: [task] };
        case "job.detail":
          return { job: task, children: [], events: [] };
        case "job.cancel":
          task.status = "cancelled";
          task.termination_source = "user_action";
          return { job: task };
        case "library.bind":
          bound = args.params.enabled;
          return {};
        case "document.preview":
          if (scenario.paging) {
            const scope = args.params.page,
              offset = args.params.offset ?? 0,
              limit = args.params.limit ?? 50,
              total = scope ? 60 : 120;
            if (scope === 2)
              await new Promise((resolve) => setTimeout(resolve, 180));
            return {
              version: doc,
              warnings: [],
              chunks: Array.from(
                { length: Math.max(0, Math.min(limit, total - offset)) },
                (_, i) => ({
                  id: `c-${scope}-${offset + i}`,
                  text: `page${scope ?? "all"}-${offset + i}`,
                  locator: { page: scope ?? 21 },
                }),
              ),
              total_chunks: total,
            };
          }
          return {
            version:
              args.params.version_id === "reviewed"
                ? { ...doc, title: "Updated title", revision: "R1" }
                : doc,
            warnings: [],
            chunks: [
              {
                id: "c",
                text: "4.1 Equipment qualification requirements.",
                locator: { page: 21 },
              },
            ],
            total_chunks: 1,
          };
        case "document.ocr.pages":
          return { pages: [], summary: {} };
        case "document.ocr.render":
          return { data_url: "" };
        case "document.history":
          return {
            versions: [
              { ...doc, id: "final", processing_stage: "final" },
              {
                ...doc,
                id: "initial",
                processing_stage: "initial",
                chunk_count: 386,
              },
            ],
          };
        case "model.list":
          return { models: [], root: "", runtime: { available: true } };
        case "ocr.model.list":
          return { models: [], runtime: { available: true } };
        case "search.start":
          return { task: { id: "library-search-task", revision: 0, attempt: 1, status: "running" } };
        case "search.status":
          return { task: { id: "library-search-task", revision: 0, attempt: 1, status: "completed" }, result: {
            evidence: [
              {
                id: "c",
                version_id: "final",
                title: "DO-160F",
                text: "4.1 Equipment qualification requirements.",
                locator: { page: 21 },
                score: 1,
              },
            ],
          } };
        default:
          throw new Error(args.action);
      }
    };
  }, scenario);
  await page.goto("http://127.0.0.1:5173");
  if (scenario.skipRag) return;
  await page.getByRole("button", { name: "System features" }).click();
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Local knowledge", exact: true })
    .click();
}
test("home separates library management from project settings", async ({
  page,
}) => {
  await setup(page);
  await expect(
    page.getByRole("button", { name: "新建资料库", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("combobox", { name: "Project", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "打开资料库 适航资料" }).click();
  await expect(
    page.getByRole("tab", { name: "文档", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("tab", { name: "检索测试", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("DO-160F.pdf", { exact: true })).toHaveCount(1);
  await expect(
    page.getByRole("button", { name: "重建检索索引", exact: true }),
  ).toHaveCount(0);
});
test("project binding always opens an actionable project selector", async ({
  page,
}) => {
  await setup(page);
  await page.getByRole("button", { name: "添加到项目", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "添加到项目" });
  await dialog.getByLabel("Project Alpha", { exact: true }).check();
  await dialog.getByRole("button", { name: "应用到所选项目" }).click();
  await expect(dialog).toHaveCount(0);
  await expect
    .poll(() =>
      page.evaluate(() =>
        window.ragCalls.some(
          (x) => x.action === "library.bind" && x.params.enabled,
        ),
      ),
    )
    .toBe(true);
});
test("library retrieval test does not require project binding", async ({
  page,
}) => {
  await setup(page);
  await page.getByRole("button", { name: "打开资料库 适航资料" }).click();
  await page.getByRole("tab", { name: "检索测试", exact: true }).click();
  await page.getByLabel("检索问题", { exact: true }).fill("设备环境要求");
  await page.getByRole("button", { name: "检索条款", exact: true }).click();
  await expect(
    page.getByText("4.1 Equipment qualification requirements.", {
      exact: true,
    }),
  ).toBeVisible();
});

test("OCR progress shows page counts and cancellation requires confirmation", async ({
  page,
}) => {
  await setup(page, { task: true });
  await page.getByRole("button", { name: /^处理任务/ }).click();
  await expect(
    page.getByText("已识别 18/64 页", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "取消任务", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "查看详情", exact: true }).click();
  await page.getByRole("button", { name: "取消任务", exact: true }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () => window.ragCalls.filter((x) => x.action === "job.cancel").length,
      ),
    )
    .toBe(0);
  await page
    .getByRole("alertdialog")
    .getByRole("button", { name: "确认取消", exact: true })
    .click();
  await expect
    .poll(() =>
      page.evaluate(
        () => window.ragCalls.filter((x) => x.action === "job.cancel").length,
      ),
    )
    .toBe(1);
});
test("import wizard explains a missing OCR package before starting", async ({
  page,
}) => {
  await setup(page);
  await page.getByRole("button", { name: "打开资料库 适航资料" }).click();
  await page.getByRole("button", { name: "导入文件", exact: true }).click();
  await page.getByRole("button", { name: "选择本地文件", exact: true }).click();
  await page.getByRole("button", { name: "下一步", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "开始导入 1 个文件", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "配置 OCR 模型", exact: true }),
  ).toBeVisible();
  await page.getByLabel("OCR 模式", { exact: true }).selectOption("off");
  await expect(
    page.getByRole("button", { name: "开始导入 1 个文件", exact: true }),
  ).toBeEnabled();
});
test("document preview remains readable in a small desktop window", async ({
  page,
}) => {
  await setup(page);
  await page.setViewportSize({ width: 900, height: 500 });
  await page.getByRole("button", { name: "打开资料库 适航资料" }).click();
  await page.getByRole("button", { name: "预览", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "解析正文", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("4.1 Equipment qualification requirements.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect
    .poll(() =>
      page.locator(".rag-reader-text").evaluate((el) => el.clientHeight),
    )
    .toBeGreaterThan(120);
});

for (const layout of [
  { width: 1002, height: 723, scale: 1 },
  { width: 900, height: 500, scale: 1 },
  { width: 1002, height: 723, scale: 2 },
]) {
  test(`PDF original and text share a row at ${layout.width}x${layout.height} scale ${layout.scale}`, async ({
    browser,
  }) => {
    const context = await browser.newContext({
      viewport: { width: layout.width, height: layout.height },
      deviceScaleFactor: layout.scale,
    });
    try {
      const page = await context.newPage();
      await setup(page);
      await page.getByRole("button", { name: "打开资料库 适航资料" }).click();
      await page.getByRole("button", { name: "预览", exact: true }).click();
      await expect(page.locator(".rag-source-page")).toBeVisible();
      const original = await page.locator(".rag-source-page").boundingBox();
      const reader = await page.locator(".rag-reader-pane").boundingBox();
      expect(Math.abs(original.y - reader.y)).toBeLessThan(2);
      expect(original.x + original.width).toBeLessThanOrEqual(reader.x);
      expect(reader.width).toBeGreaterThan(250);
    } finally {
      await context.close();
    }
  });
}

test("PDF preview uses one consistent physical-page scope for pagination", async ({
  page,
}) => {
  await setup(page, { paging: true });
  await page.getByRole("button", { name: "打开资料库 适航资料" }).click();
  await page.getByRole("button", { name: "预览", exact: true }).click();
  await expect(page.getByText("page1-0", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "加载更多片段", exact: true }).click();
  await expect(page.locator(".rag-reader-text article")).toHaveCount(60);
});
test("rapid PDF page changes reject stale earlier responses", async ({
  page,
}) => {
  await setup(page, { paging: true });
  await page.getByRole("button", { name: "打开资料库 适航资料" }).click();
  await page.getByRole("button", { name: "预览", exact: true }).click();
  const input = page.getByRole("spinbutton", { name: "物理页码" });
  await input.fill("2");
  await input.fill("3");
  await page.waitForTimeout(300);
  await expect(page.getByText("page3-0", { exact: true })).toBeVisible();
});
test("saving reviewed metadata opens the resulting immutable version", async ({
  page,
}) => {
  await setup(page);
  await page.getByRole("button", { name: "打开资料库 适航资料" }).click();
  await page.getByRole("button", { name: "预览", exact: true }).click();
  await page.getByRole("tab", { name: "文档信息", exact: true }).click();
  await page.getByRole("button", { name: "补充版本信息与复核" }).click();
  await page
    .getByRole("textbox", { name: "资料标题", exact: true })
    .fill("Updated title");
  await page.getByRole("textbox", { name: "修订版本", exact: true }).fill("R1");
  await page
    .getByRole("textbox", { name: "来源", exact: true })
    .fill("Official");
  await page
    .getByRole("textbox", { name: "文件编号", exact: true })
    .fill("DO-160");
  await page
    .getByRole("checkbox", { name: "我已核对资料内容和解析提示" })
    .check();
  await page.getByRole("button", { name: "保存复核版本", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Updated title", exact: true }),
  ).toBeVisible();
});
test("turning reprocess off removes its native decision parameter", async ({
  page,
}) => {
  await setup(page, { duplicate: true });
  await page.getByRole("button", { name: "打开资料库 适航资料" }).click();
  await page.getByRole("button", { name: "导入文件", exact: true }).click();
  await page.getByRole("button", { name: "选择本地文件" }).click();
  await page.getByRole("checkbox", { name: "重新处理此文件" }).check();
  await page.getByRole("checkbox", { name: "重新处理此文件" }).uncheck();
  await page.getByRole("button", { name: "下一步" }).click();
  await page
    .getByRole("combobox", { name: "OCR 模式", exact: true })
    .selectOption("off");
  await page.getByRole("button", { name: "开始导入 1 个文件" }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          window.ragCalls.filter((x) => x.action === "document.import").length,
      ),
    )
    .toBe(1);
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          Object.keys(
            window.ragCalls.find((x) => x.action === "document.import").params
              .decisions,
          ).length,
      ),
    )
    .toBe(0);
});

test("model configuration preserves the import selection when returning", async ({
  page,
}) => {
  await setup(page);
  await page.getByRole("button", { name: "打开资料库 适航资料" }).click();
  await page.getByRole("button", { name: "导入文件", exact: true }).click();
  await page.getByRole("button", { name: "选择本地文件" }).click();
  await page.getByRole("button", { name: "下一步" }).click();
  await page.getByRole("button", { name: "配置 OCR 模型" }).click();
  await page.getByRole("button", { name: "返回资料导入", exact: true }).click();
  await expect(
    page.getByRole("dialog", { name: "导入文件 · 适航资料" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "开始导入 1 个文件" }),
  ).toBeVisible();
});

test("project conversation can open the library chooser before visiting RAG pages", async ({
  page,
}) => {
  await setup(page, { skipRag: true });
  await page
    .getByRole("button", { name: "Implementation work", exact: true })
    .click();
  await page.getByRole("button", { name: "添加项目资料", exact: true }).click();
  await expect(
    page.getByRole("dialog", { name: "本项目使用的资料库" }),
  ).toBeVisible();
  await expect
    .poll(() =>
      page
        .locator(".rag-modal-backdrop")
        .evaluate((el) => getComputedStyle(el).position),
    )
    .toBe("fixed");
});
