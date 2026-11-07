import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

const { previewEvidence, useApp } = vi.hoisted(() => ({ previewEvidence: vi.fn(), useApp: vi.fn(() => ({ activeConversation: { project_id: "project-a" } })) }));
vi.mock("../../../services/runtime", () => ({ ragRepository: { previewEvidence } }));
vi.mock("../../../contexts/AppContext", () => ({ useApp }));
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string, values?: { start: number; end: number; total: number }) => key === "rag.evidenceRange" ? `${values?.start}–${values?.end} / ${values?.total}` : key }) }));
import { MarkdownRenderer } from "./MarkdownRenderer";
import { MarkdownLink } from "./MarkdownLink";
import { loadRagEvidencePage, RagEvidenceDialog } from "./RagEvidenceLink";

const evidenceId = "rag-12345678-1234-4abc-8def-1234567890ab";
const page = {
  id: evidenceId, library_id: "library", version_id: "version", title: "Source <img>",
  revision: "R2", clause_number: "§3.2", locator: { page: 3, paragraph: 7 },
  text: "<script>alert('source')</script>", text_hash: "hash", score: 0.8,
  offset: 0, total_length: 16001, external_untrusted: true,
};

describe("chat evidence citations", () => {
  it("renders a persisted markdown citation as a source button", () => {
    const html = renderToStaticMarkup(<MarkdownRenderer content={`[AC source](rag-evidence:${evidenceId})`} state="complete" />);
    expect(html).toContain("AC source");
    expect(html).toContain('class="markdown-link markdown-link--evidence"');
    expect(html).toContain('aria-haspopup="dialog"');
    expect(html).not.toContain('href="rag-evidence:');
  });

  it("keeps ordinary links rendering independently from evidence context", () => {
    useApp.mockImplementation(() => { throw new Error("App provider required"); });
    expect(renderToStaticMarkup(<MarkdownLink href="https://example.com">Web</MarkdownLink>)).toContain('title="https://example.com"');
    expect(renderToStaticMarkup(<MarkdownLink href="rag-evidence:invalid">Blocked</MarkdownLink>)).toContain("markdown-link--blocked");
  });

  it("escapes original text and metadata while showing source location and pagination", () => {
    const html = renderToStaticMarkup(<RagEvidenceDialog page={page} loading={false} error={null} onClose={() => {}} onPage={() => {}} />);
    expect(html).toContain('role="dialog"');
    expect(html).toContain("Source &lt;img&gt;");
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("<script>");
    expect(html).toContain("R2");
    expect(html).toContain("§3.2");
    expect(html).toContain('&quot;page&quot;: 3');
    expect(html).toMatch(/disabled="">rag.evidencePrevious/);
    expect(html).toContain(">rag.evidenceNext</button>");
  });

  it("disables next on the final page and displays unavailable sources explicitly", () => {
    const html = renderToStaticMarkup(<RagEvidenceDialog page={{ ...page, offset: 16000 }} loading={false} error={null} onClose={() => {}} onPage={() => {}} />);
    expect(html).toMatch(/disabled="">rag.evidenceNext/);
    const failed = renderToStaticMarkup(<RagEvidenceDialog page={null} loading={false} error="rag.evidenceUnavailable" onClose={() => {}} onPage={() => {}} />);
    expect(failed).toContain('role="alert"');
    expect(failed).toContain("rag.evidenceUnavailable");
  });

  it("renders escaped parent context alongside the cited fragment and pages beyond the fragment length", () => {
    const html = renderToStaticMarkup(<RagEvidenceDialog page={{ ...page, total_length: 120,
      parent_context: { text: "Continuation <img src=x onerror=alert(1)> with an exception", text_hash: "parent-page-hash", offset: 0, total_length: 17000, has_more: true, scope: "clause" },
    }} loading={false} error={null} onClose={() => {}} onPage={() => {}} />);
    expect(html).toContain("rag.evidenceFragment");
    expect(html).toContain("rag.evidenceParentContext");
    expect(html).toContain("rag.evidenceContextScopes.clause");
    expect(html).toContain("Continuation &lt;img src=x onerror=alert(1)&gt; with an exception");
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;script&gt;");
    expect(html).toContain("R2");
    expect(html).toContain("§3.2");
    expect(html).toContain("1–8000 / 17000");
    expect(html).not.toMatch(/disabled="">rag.evidenceNext/);
  });

  it("keeps parent pagination available on later pages and stops at the parent boundary", () => {
    const parentPage = { ...page, text: "Canonical cited fragment", offset: 8000, total_length: 120,
      parent_context: { text: "Second parent page", offset: 8000, total_length: 17000, has_more: true, scope: "table" },
    };
    const middle = renderToStaticMarkup(<RagEvidenceDialog page={parentPage} loading={false} error={null} onClose={() => {}} onPage={() => {}} />);
    expect(middle).toContain("Second parent page");
    expect(middle).toContain("Canonical cited fragment");
    expect(middle).toContain("8001–16000 / 17000");
    expect(middle).not.toMatch(/disabled="">rag.evidenceNext/);
    expect(middle).not.toMatch(/disabled="">rag.evidencePrevious/);
    const end = renderToStaticMarkup(<RagEvidenceDialog page={{ ...parentPage, offset: 16000,
      parent_context: { ...parentPage.parent_context, text: "Last parent page", offset: 16000, has_more: false },
    }} loading={false} error={null} onClose={() => {}} onPage={() => {}} />);
    expect(end).toContain("16001–17000 / 17000");
    expect(end).toMatch(/disabled="">rag.evidenceNext/);
  });

  it("requests at most 8000 source characters for the active project", async () => {
    previewEvidence.mockImplementationOnce(async (projectId, id, offset, length) => {
      if (projectId !== "project-a" || id !== evidenceId || offset !== 8000 || length !== 8000) throw new Error("wrong evidence scope or page range");
      return { ...page, offset: 8000 };
    });
    const result = await loadRagEvidencePage("project-a", evidenceId, 8000, new AbortController().signal);
    expect(result?.offset).toBe(8000);
    expect(result?.title).toBe("Source <img>");
  });

  it("ignores an outstanding result after the selection or project is cancelled", async () => {
    let finish!: (value: typeof page) => void;
    previewEvidence.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const controller = new AbortController();
    const pending = loadRagEvidencePage("project-a", evidenceId, 0, controller.signal);
    controller.abort();
    finish(page);
    expect(await pending).toBeNull();
  });

  it("propagates withdrawn or denied evidence failures without returning old source text", async () => {
    previewEvidence.mockRejectedValueOnce(new Error("rag_evidence_invalid"));
    await expect(loadRagEvidencePage("project-a", evidenceId, 0, new AbortController().signal)).rejects.toThrow("rag_evidence_invalid");
  });
});
