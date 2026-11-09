import { useEffect, useRef, useState } from "react";
import { Button } from "../../components/ui/Button";
import { ragRepository } from "../../services/runtime";
import type {
  RagFileInspection,
  RagLibrary,
  RagPathSelection,
} from "../../types/rag";
import { WorkspaceDialog } from "./workspaceShared";

interface ImportDraft {
  library: RagLibrary;
  selection: RagPathSelection | null;
  files: RagFileInspection[];
  mode: "auto" | "off";
  decisions: Record<string, string>;
  step: number;
  requestKey: string;
}
let suspendedImport: ImportDraft | null = null;
export function suspendedImportLibrary(): RagLibrary | null {
  return suspendedImport?.library ?? null;
}

export function ImportWizard({
  library,
  onClose,
  onStarted,
  onModels,
}: {
  library: RagLibrary;
  onClose: () => void;
  onStarted: () => void;
  onModels: (tab: "retrieval" | "ocr") => void;
}) {
  const draft =
    suspendedImport?.library.id === library.id ? suspendedImport : null;
  const suspended = useRef(false);
  const [selection, setSelection] = useState<RagPathSelection | null>(
    draft?.selection ?? null,
  );
  const selectionRef = useRef<RagPathSelection | null>(
    draft?.selection ?? null,
  );
  const [files, setFiles] = useState<RagFileInspection[]>(draft?.files ?? []);
  const [mode, setMode] = useState<"auto" | "off">(
    draft?.mode ?? library.ocr_mode ?? "auto",
  );
  const [decisions, setDecisions] = useState<Record<string, string>>(
    draft?.decisions ?? {},
  );
  const [ocrReady, setOcrReady] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const [error, setError] = useState("");
  const [step, setStep] = useState(draft?.step ?? 1);
  const requestKey = useRef(draft?.requestKey ?? crypto.randomUUID());
  useEffect(() => {
    void ragRepository
      .listOcrModels()
      .then((catalog) =>
        setOcrReady(catalog.runtime.available && catalog.models.length > 0),
      )
      .catch(() => setOcrReady(false));
    return () => {
      if (selectionRef.current && !suspended.current)
        void ragRepository
          .revokeSelection(selectionRef.current)
          .catch(() => {});
    };
  }, []);
  const hasPdf = files.some((file) => file.name.toLowerCase().endsWith(".pdf"));
  const missingOcr = hasPdf && mode === "auto" && !ocrReady;
  const unresolved = files.some(
    (file) => file.conflict_document_id && !decisions[file.path],
  );
  async function choose() {
    setBusy(true);
    setError("");
    try {
      const chosen = await ragRepository.selectPaths("documents");
      if (!chosen) return;
      if (selectionRef.current)
        await ragRepository.revokeSelection(selectionRef.current);
      selectionRef.current = chosen;
      setSelection(chosen);
      const result = await ragRepository.inspectDocuments(library.id, chosen);
      setFiles(result.files);
      setDecisions({});
      requestKey.current = crypto.randomUUID();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }
  async function start() {
    if (!selection || lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      const result = (await ragRepository.importDocuments(
        library.id,
        selection,
        {
          ocr_mode: mode,
          device: "auto",
          auto_index: true,
          request_key: requestKey.current,
          decisions: Object.fromEntries(
            Object.entries(decisions).filter(([, value]) => value),
          ),
        },
      )) as { job?: unknown; duplicates?: RagFileInspection[] };
      selectionRef.current = null;
      suspendedImport = null;
      const existing = result.duplicates?.find(
        (file) => file.duplicate,
      )?.duplicate;
      if (!result.job && existing) {
        window.dispatchEvent(
          new CustomEvent("agent:rag-navigate", {
            detail: {
              kind: "document",
              libraryId: library.id,
              versionId: existing.id,
            },
          }),
        );
        onClose();
      } else onStarted();
    } catch (reason) {
      setError(String(reason) + "。可重新选择文件后重试。");
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }
  return (
    <WorkspaceDialog
      title={`导入文件 · ${library.name}`}
      onClose={() => {
        if (!busy) {
          suspendedImport = null;
          onClose();
        }
      }}
    >
      <ol className="rag-steps">
        <li aria-current={step === 1 ? "step" : undefined}>1 选择文件</li>
        <li aria-current={step === 2 ? "step" : undefined}>2 确认处理方式</li>
      </ol>
      <p>
        目标资料库 <strong>{library.name}</strong>
      </p>
      {error && <p role="alert">{error}</p>}
      {step === 1 ? (
        <>
          <Button disabled={busy} onClick={() => void choose()}>
            {busy ? "正在校验文件…" : "选择本地文件"}
          </Button>
          {files.map((file) => (
            <article className="rag-import-file" key={file.path}>
              <strong>{file.name}</strong>
              <span>{(file.size_bytes / 1024 ** 2).toFixed(1)} MiB</span>
              {file.duplicate && (
                <>
                  <p>该文件已存在。默认跳过重复文件。</p>
                  <label className="rag-check">
                    <input
                      type="checkbox"
                      checked={decisions[file.path] === "reprocess"}
                      onChange={(e) =>
                        setDecisions({
                          ...decisions,
                          [file.path]: e.target.checked ? "reprocess" : "",
                        })
                      }
                    />
                    重新处理此文件
                  </label>
                  <Button
                    onClick={() => {
                      window.dispatchEvent(
                        new CustomEvent("agent:rag-navigate", {
                          detail: {
                            kind: "document",
                            libraryId: library.id,
                            versionId: file.duplicate!.id,
                          },
                        }),
                      );
                      onClose();
                    }}
                  >
                    查看已有文档
                  </Button>
                </>
              )}
              {file.conflict_document_id && (
                <label>
                  检测到同名来源内容变化
                  <select
                    value={decisions[file.path] ?? ""}
                    onChange={(e) =>
                      setDecisions({
                        ...decisions,
                        [file.path]: e.target.value,
                      })
                    }
                  >
                    <option value="">选择版本关系</option>
                    <option value="replace">替换为新版本</option>
                    <option value="separate">作为独立文档</option>
                  </select>
                </label>
              )}
            </article>
          ))}
          <Button
            variant="primary"
            disabled={busy || !files.length || unresolved}
            onClick={() => setStep(2)}
          >
            下一步
          </Button>
          {unresolved && <p>请先确定发生变化的文件与已有资料的版本关系。</p>}
        </>
      ) : (
        <>
          <p>
            已选择 {files.length} 个文件。完成解析后自动建立一次最终检索索引。
          </p>
          <label>
            OCR 模式
            <select
              aria-label="OCR 模式"
              value={mode}
              onChange={(e) => setMode(e.target.value as "auto" | "off")}
            >
              <option value="auto">自动补充图片文字与表格</option>
              <option value="off">仅提取已有文字</option>
            </select>
          </label>
          {mode === "off" && hasPdf && (
            <p role="status">图片内容不会识别，处理结果会注明识别范围。</p>
          )}
          {missingOcr && (
            <div role="alert">
              <p>
                {ocrReady === null
                  ? "正在检查 OCR 环境…"
                  : "缺少可用的 OCR 模型包。配置后继续，或选择仅提取已有文字。"}
              </p>
              <Button
                onClick={() => {
                  suspendedImport = {
                    library,
                    selection,
                    files,
                    mode,
                    decisions,
                    step,
                    requestKey: requestKey.current,
                  };
                  suspended.current = true;
                  onModels("ocr");
                }}
              >
                配置 OCR 模型
              </Button>
            </div>
          )}
          {(!library.embedding_model_id || !library.reranker_model_id) && (
            <p>检索模型尚未配置。文件可先解析，建立索引时将提示前往配置。</p>
          )}
          <div className="rag-actions">
            <Button disabled={busy} onClick={() => setStep(1)}>
              上一步
            </Button>
            <Button
              variant="primary"
              disabled={busy || missingOcr}
              onClick={() => void start()}
            >
              {busy ? "正在创建导入任务…" : `开始导入 ${files.length} 个文件`}
            </Button>
          </div>
        </>
      )}
    </WorkspaceDialog>
  );
}
