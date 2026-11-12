import { useEffect, useRef, useState } from "react";
import { Button } from "../../components/ui/Button";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { ragRepository } from "../../services/runtime";
import type {
  OcrPage,
  RagDocument,
  RagDocumentConfirmation,
  RagDocumentPreview,
  RagLibrary,
} from "../../types/rag";
import { dateLabel } from "./workspaceShared";

export function DocumentViewer({
  library,
  versionId,
  onBack,
  onVersion,
  onChanged,
}: {
  library: RagLibrary;
  versionId: string;
  onBack: () => void;
  onVersion: (id: string) => void;
  onChanged: () => Promise<void>;
}) {
  const [preview, setPreview] = useState<RagDocumentPreview | null>(null);
  const [page, setPage] = useState(1);
  const [tab, setTab] = useState<"text" | "ocr" | "info" | "history">("text");
  const [image, setImage] = useState("");
  const [ocr, setOcr] = useState<OcrPage | null>(null);
  const [history, setHistory] = useState<(RagDocument & { id: string })[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [withdraw, setWithdraw] = useState(false);
  const [metadata, setMetadata] = useState<RagDocumentConfirmation | null>(
    null,
  );
  const pageRequest = useRef(0);
  useEffect(() => {
    let alive = true;
    setPreview(null);
    setImage("");
    setPage(1);
    setError("");
    void (async () => {
      const metadata = await ragRepository.previewDocument(versionId, 0, 1);
      const isPdf = metadata.version.media_type === "application/pdf" || /\.pdf$/i.test(metadata.version.original_name ?? "");
      const result = await ragRepository.previewDocument(versionId, 0, 50, isPdf ? 1 : undefined);
      if (alive) setPreview(result);
    })().catch((e) => { if (alive) setError(String(e)); });
    return () => {
      alive = false;
    };
  }, [versionId]);
  const version = preview?.version;
  const pdf =
    !!version &&
    (version.media_type === "application/pdf" ||
      /\.pdf$/i.test(version.original_name ?? ""));
  useEffect(() => {
    if (!pdf || !version) return;
    let alive = true;
    setImage("");
    setOcr(null);
    void ragRepository
      .renderOcrPage(versionId, page)
      .then((result) => {
        if (alive) setImage(result.data_url);
      })
      .catch((e) => {
        if (alive) setError(String(e));
      });
    void ragRepository
      .ocrPages(versionId, page)
      .then((result) => {
        if (alive)
          setOcr(result.pages.find((item) => item.page === page) ?? null);
      })
      .catch((e) => {
        if (alive) setError(String(e));
      });
    return () => {
      alive = false;
    };
  }, [pdf, versionId, page, !!version]);
  useEffect(() => {
    if (tab !== "history" || !version) return;
    let alive = true;
    void ragRepository
      .history(version.document_id ?? version.id)
      .then((result) => {
        if (alive) setHistory(result.versions);
      })
      .catch((e) => {
        if (alive) setError(String(e));
      });
    return () => {
      alive = false;
    };
  }, [tab, version?.id]);
  async function act(operation: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await operation();
      await onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function selectPage(number: number) {
    const revision = ++pageRequest.current;
    setPage(number);
    setBusy(true);
    setError("");
    try {
      const result = await ragRepository.previewDocument(versionId, 0, 50, number);
      if (pageRequest.current === revision) setPreview(result);
    } catch (e) {
      if (pageRequest.current === revision) setError(String(e));
    } finally {
      if (pageRequest.current === revision) setBusy(false);
    }
  }
  const summary = version?.ocr_summary;
  const chunks = preview?.chunks ?? [];
  return (
    <section className="rag-document-viewer">
      <header className="rag-heading">
        <div>
          <Button onClick={onBack}>返回资料库</Button>
          <h2>{version?.title ?? "文档预览"}</h2>
          <p>
            {version?.revision || "版本未标注"} ·{" "}
            {version?.human_reviewed ? "人工已复核" : "机器解析 · 待专业复核"}
          </p>
        </div>
        <div className="rag-actions">
          <Button
            disabled={!version || busy}
            onClick={() =>
              void act(() => ragRepository.openOriginal(versionId))
            }
          >
            打开本地原件
          </Button>
          <Button
            disabled={!version || version.status === "withdrawn" || busy}
            onClick={() => setWithdraw(true)}
          >
            撤回文档
          </Button>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      {!preview && !error && <p role="status">正在读取文档…</p>}
      {version && (
        <>
          <p className="rag-status">
            {version.status === "withdrawn"
              ? "已撤回"
              : version.adopted
                ? "当前用于检索"
                : version.processing_stage === "initial"
                  ? "处理中预览"
                  : "解析结果"}{" "}
            · {version.chunk_count} 片段 ·{" "}
            {summary?.coverage === "complete"
              ? `OCR 已完成 ${summary.ocr_pages ?? 0} 页`
              : summary?.coverage === "partial"
                ? "识别范围不完整"
                : "原生文字解析"}
          </p>
          <div className="rag-viewer-grid">
            {pdf && (
              <section className="rag-source-page" aria-label="PDF 原页">
                <label>
                  物理页码
                  <input
                    type="number"
                    min={1}
                    max={summary?.total_pages ?? 10000}
                    value={page}
                    onChange={(e) => {
                      const value = Number(e.target.value);
                      if (
                        Number.isInteger(value) &&
                        value >= 1 &&
                        value <= (summary?.total_pages ?? 10000)
                      )
                        void selectPage(value);
                    }}
                  />
                </label>
                {summary?.total_pages && <p>共 {summary.total_pages} 页</p>}
                {image ? (
                  <img src={image} alt={`原件物理第 ${page} 页`} />
                ) : (
                  <p>原页读取中…</p>
                )}
              </section>
            )}
            <section className="rag-reader-pane">
              <nav className="rag-tabs" role="tablist" aria-label="文档内容">
                {(
                  [
                    ["text", "解析正文"],
                    ["ocr", "OCR 对照"],
                    ["info", "文档信息"],
                    ["history", "版本历史"],
                  ] as const
                ).map(([id, label]) => (
                  <Button
                    key={id}
                    role="tab"
                    aria-selected={tab === id}
                    onClick={() => setTab(id)}
                  >
                    {label}
                  </Button>
                ))}
              </nav>
              {tab === "text" && (
                <>
                  <h3>解析正文</h3>
                  <p>外部资料 · 请核对适用性和原页。</p>
                  <div className="rag-reader-text">
                    {chunks.length ? (
                      chunks.map((chunk, index) => (
                        <article key={chunk.id ?? index}>
                          <small>
                            {typeof chunk.locator === "object"
                              ? `第 ${chunk.locator.page ?? "—"} 页 · ${chunk.clause_number ?? ""}`
                              : chunk.locator}
                          </small>
                          <pre>{chunk.text}</pre>
                        </article>
                      ))
                    ) : (
                      <p>本页尚无解析文字。可切换原页或查看处理任务。</p>
                    )}
                  </div>
                  {chunks.length < (preview.total_chunks ?? chunks.length) && (
                    <Button
                      disabled={busy}
                      onClick={() =>
                        void act(async () => {
                          const next = await ragRepository.previewDocument(
                            versionId,
                            chunks.length,
                            50,
                            pdf ? page : undefined,
                          );
                          setPreview({
                            ...next,
                            chunks: [...chunks, ...next.chunks],
                          });
                        })
                      }
                    >
                      加载更多片段
                    </Button>
                  )}
                </>
              )}
              {tab === "ocr" && (
                <>
                  <h3>第 {page} 页识别内容</h3>
                  <p>OCR 为机器识别结果，表格、数值和单位需核对原图。</p>
                  {ocr?.regions.length ? (
                    ocr.regions.map((region) => (
                      <article key={region.id}>
                        <strong>{region.kind}</strong>
                        {region.table ? (
                          <div className="ocr-table">
                            <table>
                              <tbody>
                                {region.table.rows.map((row, r) => (
                                  <tr key={r}>
                                    {row.map((cell, c) => (
                                      <td key={c}>{cell}</td>
                                    ))}
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        ) : (
                          <pre>{region.text}</pre>
                        )}
                        {region.quality_warning && (
                          <p>{region.quality_warning}</p>
                        )}
                      </article>
                    ))
                  ) : (
                    <p>本页暂无 OCR 区域，请结合识别范围查看。</p>
                  )}
                  <p>待识别页 {summary?.pending_pages?.join("、") || "无"}</p>
                  <p>失败页 {summary?.failed_pages?.join("、") || "无"}</p>
                </>
              )}
              {tab === "info" && (
                <>
                  <h3>文档信息</h3>
                  <dl>
                    <dt>来源</dt>
                    <dd>{version.source || "未填写"}</dd>
                    <dt>文件编号</dt>
                    <dd>{version.document_number || "未填写"}</dd>
                    <dt>生成时间</dt>
                    <dd>{dateLabel(version.created_at)}</dd>
                    <dt>识别范围</dt>
                    <dd>{summary?.coverage ?? "原生文字"}</dd>
                  </dl>
                  <h4>解析提示</h4>
                  {preview.warnings.map((warning, index) => (
                    <p key={index}>
                      {warning ===
                      "OCR is running. Image regions are not yet included."
                        ? "这是原生文字初始快照。图片内容尚未纳入，请查看后台任务。"
                        : warning}
                    </p>
                  ))}
                  <Button
                    disabled={busy || version.status === "withdrawn"}
                    onClick={() =>
                      setMetadata({
                        version_id: versionId,
                        title: version.title,
                        revision: version.revision || "",
                        source: version.source || "",
                        document_number: version.document_number || "",
                        warnings_acknowledged: false,
                      })
                    }
                  >
                    补充版本信息与复核
                  </Button>
                  {metadata && (
                    <form
                      onSubmit={(e) => {
                        e.preventDefault();
                        void act(async () => {
                          const result = await ragRepository.updateMetadata(metadata) as {version_id:string};
                          setMetadata(null);
                          onVersion(result.version_id);
                        });
                      }}
                    >
                      <p>
                        保存后生成新的复核版本并建立索引，原版本保存在历史中。
                      </p>
                      {(
                        [
                          ["title", "资料标题"],
                          ["revision", "修订版本"],
                          ["source", "来源"],
                          ["document_number", "文件编号"],
                        ] as const
                      ).map(([key, label]) => (
                        <label key={key}>
                          {label}
                          <input
                            required
                            maxLength={500}
                            value={metadata[key]}
                            onChange={(e) =>
                              setMetadata({
                                ...metadata,
                                [key]: e.target.value,
                              })
                            }
                          />
                        </label>
                      ))}
                      <label className="rag-check">
                        <input
                          type="checkbox"
                          checked={metadata.warnings_acknowledged}
                          onChange={(e) =>
                            setMetadata({
                              ...metadata,
                              warnings_acknowledged: e.target.checked,
                            })
                          }
                        />
                        我已核对资料内容和解析提示
                      </label>
                      <Button
                        type="submit"
                        disabled={busy || !metadata.warnings_acknowledged}
                      >
                        保存复核版本
                      </Button>
                    </form>
                  )}
                </>
              )}
              {tab === "history" && (
                <>
                  <h3>版本历史</h3>
                  {history.map((item) => (
                    <article className="rag-history-row" key={item.id}>
                      <strong>
                        {item.revision || "未标注"} ·{" "}
                        {item.processing_stage === "initial"
                          ? "原生文字初始快照"
                          : "处理完成版本"}
                      </strong>
                      <p>
                        {dateLabel(item.created_at)} · {item.chunk_count} 片段 ·{" "}
                        {item.status === "withdrawn"
                          ? "已撤回"
                          : item.adopted
                            ? "当前采用"
                            : "历史记录"}{" "}
                        · {item.ocr_summary?.coverage ?? "原生文字"}
                      </p>
                      <Button onClick={() => onVersion(item.id)}>
                        查看此版本
                      </Button>
                    </article>
                  ))}
                </>
              )}
            </section>
          </div>
          <ConfirmDialog
            open={withdraw}
            title="撤回此文档版本？"
            description={`该版本将停止参与新检索。所属资料库“${library.name}”，已启用该库的项目有 ${library.project_count ?? 0} 个。历史引用将显示来源状态。`}
            confirmLabel="确认撤回"
            busy={busy}
            onCancel={() => setWithdraw(false)}
            onConfirm={() =>
              void act(async () => {
                await ragRepository.withdrawDocument(versionId);
                setWithdraw(false);
                onBack();
              })
            }
          />
        </>
      )}
    </section>
  );
}
