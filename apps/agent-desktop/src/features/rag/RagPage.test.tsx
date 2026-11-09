import { expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { navigationItems, pageTitleKeys } from "../../app/navigation";
vi.mock("../../services/runtime", () => ({ ragRepository: {
  listModels: vi.fn().mockResolvedValue({ models: [], root: "", runtime: {} }),
  listLibraries: vi.fn().mockResolvedValue({ libraries: [] }),
  listJobs: vi.fn().mockResolvedValue({ jobs: [] }),
} }));
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock("../../contexts/AppContext", () => ({ useApp: () => ({
  projects: [], viewingProjectId: "project", activeConversation: null, draft: { projectId: null },
}) }));
import { DocumentReview, IndexVersionPicker, formatRagLocator, RagPage } from "./RagPage";
import type { RagDocumentPreview, RagLibrary } from "../../types/rag";
import { loadRagPageData } from "./ragPageData";
import { ragRepository } from "../../services/runtime";

const preview: RagDocumentPreview = {
  version: { id: "version", version_id: "version", title: "AC source", revision: "R1", source: "CAAC", document_number: "AC-1", status: "pending", chunk_count: 201, warnings: ["Table extraction requires review"] },
  chunks: [{ id: "chunk", text: "<script>alert('source')</script>", locator: { page: 3, paragraph: 7 } }],
  warnings: ["Table extraction requires review"], total_chunks: 201,
};
it("exposes the real knowledge navigation route", () => {
  expect(navigationItems.find((item) => item.id === "rag")?.availability).not.toBe("planned");
  expect(pageTitleKeys.rag).toBe("rag.title");
});
it("renders source warnings, escaped passages and review controls with confirmation gated", () => {
  const html = renderToStaticMarkup(<DocumentReview preview={preview} busy={false} onClose={() => {}} onMore={() => {}} onConfirm={() => {}} />);
  expect(html).toContain('role="dialog"');
  expect(html).toContain("Table extraction requires review");
  expect(html).toContain("&lt;script&gt;");
  expect(html).not.toContain("<script>");
  expect(html).toMatch(/disabled="">rag.confirmDocument/);
  expect(html).toContain("rag.loadMore");
  expect(html).toContain("3 · 7");
});
it("formats structured and legacy string source locations", () => {
  expect(formatRagLocator("page 3")).toBe("page 3");
  expect(formatRagLocator({ page: 2, heading: "§3", unsafe: { path: "C:/secret" } })).toBe("2 · §3");
  expect(formatRagLocator(undefined)).toBe("");
});

it("describes saved initial OCR snapshots without claiming the task is still running", () => {
  const html = renderToStaticMarkup(<DocumentReview preview={{ ...preview, warnings: ["OCR is running. Image regions are not yet included."] }} busy={false} onClose={() => {}} onMore={() => {}} onConfirm={() => {}} />);
  expect(html).toContain("rag.ocrInitialSnapshot");
  expect(html).not.toContain("OCR is running");
});
it("gates index construction until duplicate confirmed revisions have a choice", () => {
  const document = { ...preview.version, id: "document", status: "confirmed" };
  const library: RagLibrary = { id: "library", name: "Library", embedding_model_id: "embedding", reranker_model_id: "ranking", active_index_id: null, generation: 1, documents: [{ ...document, version_id: "v1", revision: "R1" }, { ...document, version_id: "v2", revision: "R2" }] };
  const html = renderToStaticMarkup(<IndexVersionPicker library={library} busy={false} onBuild={() => {}} />);
  expect(html).toContain("rag.chooseRevisionRequired");
  expect(html).toContain("rag.excludeDocument");
  expect(html).toMatch(/disabled="">rag.index/);
  expect(html.match(/type="radio"/g)).toHaveLength(3);
  expect(html).not.toContain('checked=""');
});

it("shows cached library data immediately without a blocking processing indicator", async () => {
  vi.stubGlobal("window", { sessionStorage: { getItem: () => null, removeItem: () => {} } });
  await loadRagPageData(ragRepository, "project");
  const html = renderToStaticMarkup(<RagPage />);
  expect(html).not.toContain("rag.loading");
  expect(html).not.toMatch(/disabled="">rag.refresh/);
  vi.unstubAllGlobals();
});

it("keeps the initial loading indicator when this project has no display snapshot", async () => {
  vi.stubGlobal("window", { sessionStorage: { getItem: () => null, removeItem: () => {} } });
  await loadRagPageData(ragRepository, "another-project");
  expect(renderToStaticMarkup(<RagPage />)).toContain("rag.loading");
  vi.unstubAllGlobals();
});
