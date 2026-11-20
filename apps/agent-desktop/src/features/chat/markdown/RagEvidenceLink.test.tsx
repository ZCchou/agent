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
    const html = renderToStaticMarkup(<MarkdownRenderer content={`[AC source](rag-evidence:${evidenceId})`} state="complete" verifiedEvidenceIds={[evidenceId]} />);
    expect(html).toContain("AC source");
    expect(html).toContain('class="markdown-link markdown-link--evidence"');
    expect(html).toContain('aria-haspopup="dialog"');
    expect(html).not.toContain('href="rag-evidence:');
  });

  it("keeps unverified and other-message citation text inert without deleting prose", () => {
    const content = `Public answer [source](rag-evidence:${evidenceId}) remains visible.`;
    const unverified = renderToStaticMarkup(<MarkdownRenderer content={content} state="complete" />);
    expect(unverified).toContain("Public answer");
    expect(unverified).toContain("remains visible");
    expect(unverified).not.toContain('aria-haspopup="dialog"');
    const other = renderToStaticMarkup(<MarkdownRenderer content={content} state="complete" verifiedEvidenceIds={["rag-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]} />);
    expect(other).not.toContain('aria-haspopup="dialog"');
  });

  it("shows HTML-shaped public prose as inert text rather than deleting it", () => {
    const html = renderToStaticMarkup(<MarkdownRenderer content={"<div>Public HTML-shaped paragraph</div>"} state="complete" />);
    expect(html).toContain("Public HTML-shaped paragraph");
    expect(html).toContain("&lt;div&gt;");
    expect(html).not.toContain("<div>Public HTML");
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
it("decorates explicit document verdicts and approved numeric citations while preserving ordinary chat prose", () => {
 const report = "## p-7\n\n**判断**：无法判断。\n\n> **判断**：不符合\n\n|判断|原文|\n|--|--|\n|待补充|符合|";
 const preview = renderToStaticMarkup(<MarkdownRenderer content={report} state="draft" documentReview="preview" targetLabels={{"p-7":"低温试验"}} />);
 expect(preview).toContain('title="p-7">低温试验');
 expect(preview).toContain('无法判断');
 expect(preview).toContain('待补充');
 expect(preview).not.toContain('review-verdict');
 const content = renderToStaticMarkup(<MarkdownRenderer content={report} state="draft" documentReview="committed" targetLabels={{"p-7":"低温试验"}} />);
 expect(content).toContain('title="p-7">低温试验');
 expect(content).toContain('review-verdict--unknown');
 expect(content).toContain('review-verdict--missing');
 expect(content).not.toContain('review-verdict--issue');
 expect(content).not.toContain('review-verdict--pass');
 expect(renderToStaticMarkup(<MarkdownRenderer content={report} state="complete" />)).not.toContain('review-verdict');
 const persisted="# 文档审阅正文\n\n"+report;
 for (const state of ["streaming", "draft"] as const) {
   expect(renderToStaticMarkup(<MarkdownRenderer content={persisted} state={state} />)).not.toContain("review-verdict");
 }
 expect(renderToStaticMarkup(<MarkdownRenderer content={persisted} state="complete" />)).toContain("review-verdict--unknown");
 const id="rag-12345678-1234-1234-1234-123456789012";
 useApp.mockReturnValue({activeConversation:{project_id:"project-a"}});
 const cite=`原文[1](rag-evidence:${id})`;
 expect(renderToStaticMarkup(<MarkdownRenderer content={cite} state="complete" verifiedEvidenceIds={[id]} />)).toContain('markdown-link--citation');
 expect(renderToStaticMarkup(<MarkdownRenderer content={cite} state="complete" />)).not.toContain('markdown-link--citation');
});
it("opens only a matching local report source; the same source number is scoped to its own message", () => {
 const report="# 文档审阅正文\n\n正文[1](#审阅出处-1)[2](#审阅出处-2)\n\n##### 审阅出处 1\n\n**来源**　标准 · §4.6.2 · PDF 第 18 页\n\n> 减压在 15s 内完成。";
 const html=renderToStaticMarkup(<><MarkdownRenderer content={report} state="complete" /><MarkdownRenderer content={report} state="complete" /></>);
 const ids=[...html.matchAll(/<details id="([^"]+)"/g)].map(m=>m[1]);
 expect(new Set(ids).size).toBe(2);
 for(const id of ids) expect(html).toContain('aria-controls="'+id+'"');
 expect(html).not.toContain('aria-label="审阅出处 2"');
 expect(html).toContain('<summary>[1] 标准 · §4.6.2 · PDF 第 18 页</summary>');
});
it("keeps the generated summary an index, preserves its citation and colors verdicts in requested tables", () => {
 const table="| 问题项及位置 | 判断 | 原文 | 建议 | 证据 |\n|---|---|---|---|---|\n| 减压 | 待补充 | 2h | 说明依据 | [1](#审阅出处-1) |";
 const report="# 文档审阅正文\n\n## 问题与待核事项汇总\n\n"+table+"\n\n## 减压\n\n**判断**：待补充。\n\n## 用户表格\n\n"+table+
   "\n\n## 问题与待核事项汇总\n\n| 位置 | 判断 |\n|---|---|\n| 第二处 | 无法判断 |\n\n##### 审阅出处 1\n\n**来源**　标准 · 第 18 页\n\n> 摘录正文。";
 const html=renderToStaticMarkup(<MarkdownRenderer content={report} state="complete" />);
 const tables=[...html.matchAll(/<table\b[^>]*>([\s\S]*?)<\/table>/g)].map(m=>m[1]);
 expect(tables).toHaveLength(3);
 expect(tables[0]).toContain("<td>待补充</td>");
 expect(tables[0]).not.toContain("review-verdict");
 expect(tables[0]).toContain('aria-label="审阅出处 1"');
 expect(tables[1]).toContain('review-verdict--missing');
 expect(tables[2]).toContain('review-verdict--unknown');
 expect(html.match(/review-verdict--missing/g)).toHaveLength(2);
});
