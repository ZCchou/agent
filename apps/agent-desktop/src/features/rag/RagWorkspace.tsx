import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, ListTodo, Plus, RefreshCw, Upload } from 'lucide-react';
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { useApp } from "../../contexts/AppContext";
import { ragRepository } from "../../services/runtime";
import type {
  RagDocument,
  RagEvidence,
  RagJob,
  RagLibrary,
  RagModelList,
} from "../../types/rag";
import { IndexVersionPicker } from "./RagPage";
import { ImportWizard, suspendedImportLibrary } from "./ImportWizard";
import { DocumentViewer } from "./DocumentViewer";
import { BindProjectDialog } from "./ProjectLibraries";
import { TaskCenter, TaskProgress } from "./RagTasks";
import {
  activeTask,
  dateLabel,
  modelDestination,
  WorkspaceDialog,
  type RagView,
} from "./workspaceShared";
import "./workspace.css";

const displayCache = {
  libraries: [] as RagLibrary[],
  jobs: [] as RagJob[],
  loaded: false,
};
const detailCache = new Map<string, { query: string; withdrawn: boolean }>();
let listQuery = "";
const searchCache = new Map<string, { query: string; items: RagEvidence[] }>();
const savedScrolls = new Map<string, number>();
function initialView(): RagView {
  try {
    const raw = window.sessionStorage.getItem("agent:rag-view");
    if (raw) return JSON.parse(raw) as RagView;
  } catch {
    /* Start from the library list when saved navigation is unavailable. */
  }
  return { kind: "home" };
}

export function RagWorkspace() {
  const { setCurrentPage, projects } = useApp();
  const [view, setView] = useState<RagView>(initialView);
  const [libraries, setLibraries] = useState<RagLibrary[]>(
    displayCache.libraries,
  );
  const [jobs, setJobs] = useState<RagJob[]>(displayCache.jobs);
  const [loading, setLoading] = useState(!displayCache.loaded);
  const [query, setQuery] = useState(listQuery);
  const [error, setError] = useState("");
  const [create, setCreate] = useState(false);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const createLock = useRef(false);
  const [binding, setBinding] = useState<RagLibrary | null>(null);
  const [importing, setImporting] = useState<RagLibrary | null>(
    suspendedImportLibrary,
  );
  const alive = useRef(true);
  const refreshRevision = useRef(0);
  const refresh = useCallback(async () => {
    const revision = ++refreshRevision.current;
    const [catalog, tasks] = await Promise.all([
      ragRepository.overview(),
      ragRepository.listJobs(),
    ]);
    if (!alive.current || revision !== refreshRevision.current) return;
    displayCache.libraries = catalog.libraries;
    displayCache.jobs = tasks.jobs;
    displayCache.loaded = true;
    setLibraries(catalog.libraries);
    setJobs(tasks.jobs);
    setLoading(false);
    setError("");
  }, []);
  useEffect(() => {
    alive.current = true;
    void refresh().catch((e) => {
      if (alive.current) {
        setError(String(e));
        setLoading(false);
      }
    });
    const navigate = (event: Event) =>
      setView((event as CustomEvent<RagView>).detail);
    window.addEventListener("agent:rag-navigate", navigate);
    return () => {
      alive.current = false;
      window.removeEventListener("agent:rag-navigate", navigate);
    };
  }, [refresh]);
  useEffect(() => {
    if (!jobs.some(activeTask)) return;
    const timer = window.setInterval(() => {
      void refresh().catch((e) => {
        if (alive.current) setError(String(e));
      });
    }, 2000);
    return () => window.clearInterval(timer);
  }, [jobs, refresh]);
  const scrolls = useRef(savedScrolls);
  useEffect(() => {
    window.sessionStorage.setItem("agent:rag-view", JSON.stringify(view));
    const container =
      document.querySelector<HTMLElement>(".page-container") ??
      document.querySelector<HTMLElement>("main");
    const key = JSON.stringify(view);
    if (container) container.scrollTop = scrolls.current.get(key) ?? 0;
    return () => {
      if (container) scrolls.current.set(key, container.scrollTop);
    };
  }, [view]);
  async function createLibrary() {
    if (createLock.current || !name.trim()) return;
    createLock.current = true;
    setCreating(true);
    setError("");
    try {
      const result = (await ragRepository.createLibrary({
        name: name.trim(),
      })) as { library: RagLibrary };
      await refresh();
      setCreate(false);
      setName("");
      setView({ kind: "library", id: result.library.id, tab: "documents" });
    } catch (e) {
      setError(String(e));
    } finally {
      createLock.current = false;
      setCreating(false);
    }
  }
  const libraryId =
    view.kind === "library"
      ? view.id
      : view.kind === "document"
        ? view.libraryId
        : undefined;
  const library = libraries.find((item) => item.id === libraryId);
  const goDocument = (id: string, versionId: string) =>
    setView({ kind: "document", libraryId: id, versionId });
  const modelPage = (tab: "retrieval" | "ocr") =>
    modelDestination(tab, setCurrentPage);
  return (
    <div className="rag-workspace">
      <header className="rag-heading">
        <div>
          <h2>
            {view.kind === "home"
              ? "资料库"
              : view.kind === "tasks"
                ? "处理任务"
                : (library?.name ?? "资料库")}
          </h2>
          <p>
            {view.kind === "home"
              ? "管理资料，导入完成后在项目中启用。"
              : "文档、识别范围和项目使用状态分别管理。"}
          </p>
        </div>
        <div className="rag-actions">
          <Button icon={<ListTodo />} onClick={() => setView({ kind: "tasks" })}>
            处理任务
            {jobs.filter((job) => !job.parent_id && activeTask(job)).length
              ? ` · ${jobs.filter((job) => !job.parent_id && activeTask(job)).length}`
              : ""}
          </Button>
          <Button
            icon={<RefreshCw />}
            onClick={() => void refresh().catch((e) => setError(String(e)))}
          >
            刷新
          </Button>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      {loading && <p role="status">首次读取资料库…</p>}
      {view.kind === "home" && (
        <>
          <div className="rag-heading">
            <label className="rag-search-field">
              查找资料库
              <input
                value={query}
                placeholder="按名称查找"
                onChange={(e) => {
                  setQuery(e.target.value);
                  listQuery = e.target.value;
                }}
              />
            </label>
            <Button icon={<Plus />} variant="primary" onClick={() => setCreate(true)}>
              新建资料库
            </Button>
          </div>
          {!loading && !libraries.length && (
            <Card className="rag-empty">
              <h3>创建第一个资料库</h3>
              <p>将规章、标准和业务资料整理后，用于项目条款检索。</p>
              <Button onClick={() => setCreate(true)}>创建资料库</Button>
            </Card>
          )}
          <div className="rag-library-grid">
            {libraries
              .filter((item) =>
                item.name.toLowerCase().includes(query.toLowerCase()),
              )
              .map((item) => (
                <Card className="rag-library-card" key={item.id}>
                  <Button
                    className="rag-library-open"
                    aria-label={`打开资料库 ${item.name}`}
                    onClick={() =>
                      setView({
                        kind: "library",
                        id: item.id,
                        tab: "documents",
                      })
                    }
                  >
                    <strong>{item.name}</strong>
                  </Button>
                  <p>
                    {item.document_count ?? 0} 份文档 · {item.ready_count ?? 0}{" "}
                    份可检索
                  </p>
                  <p>
                    {item.processing_count
                      ? `${item.processing_count} 个任务处理中`
                      : item.attention_count
                        ? `${item.attention_count} 个任务需要处理`
                        : item.active_index_id
                          ? "资料已就绪"
                          : "等待导入或建立索引"}
                  </p>
                  <p>{item.project_count ?? 0} 个项目已启用</p>
                  <Button onClick={() => setBinding(item)}>添加到项目</Button>
                </Card>
              ))}
          </div>
        </>
      )}
      {view.kind === "tasks" && (
        <>
          <Button icon={<ArrowLeft />} onClick={() => setView({ kind: "home" })}>
            返回资料库列表
          </Button>
          <TaskCenter
            jobs={jobs}
            libraries={libraries}
            libraryId={view.libraryId}
            onRefresh={refresh}
            onOpenDocument={goDocument}
            onModels={modelPage}
          />
        </>
      )}
      {(view.kind === "library" || view.kind === "document") &&
        !library &&
        !loading && (
          <>
            <p>该资料库不可用或已移除。</p>
            <Button icon={<ArrowLeft />} onClick={() => setView({ kind: "home" })}>
              返回资料库列表
            </Button>
          </>
        )}
      {library && view.kind === "document" && (
        <DocumentViewer
          key={view.versionId}
          library={library}
          versionId={view.versionId}
          onBack={() =>
            setView({ kind: "library", id: library.id, tab: "documents" })
          }
          onVersion={(id) => goDocument(library.id, id)}
          onChanged={refresh}
        />
      )}
      {library && view.kind === "library" && (
        <>
          <div className="rag-detail-heading">
            <Button icon={<ArrowLeft />} onClick={() => setView({ kind: "home" })}>
              返回资料库列表
            </Button>
            <p>
              {library.ready_count ?? 0} 份文档可检索 ·{" "}
              {library.processing_count ?? 0} 个任务处理中
            </p>
            <Button icon={<Upload />} variant="primary" onClick={() => setImporting(library)}>
              导入文件
            </Button>
          </div>
          {(!library.embedding_model_id || !library.reranker_model_id) && (
            <div className="rag-attention">
              <p>尚未配置检索模型。可先导入资料，配置后继续建立索引。</p>
              <Button onClick={() => modelPage("retrieval")}>前往配置</Button>
            </div>
          )}
          <nav className="rag-tabs" role="tablist" aria-label="资料库详情">
            {(
              [
                ["documents", "文档"],
                ["search", "检索测试"],
                ["projects", "使用项目"],
                ["settings", "设置"],
              ] as const
            ).map(([id, label]) => (
              <Button
                role="tab"
                aria-selected={view.tab === id}
                key={id}
                onClick={() =>
                  setView({ kind: "library", id: library.id, tab: id })
                }
              >
                {label}
              </Button>
            ))}
          </nav>
          <section
            role="tabpanel"
            aria-label={
              {
                documents: "文档",
                search: "检索测试",
                projects: "使用项目",
                settings: "设置",
              }[view.tab]
            }
          >
            {view.tab === "documents" && (
              <DocumentList
                library={library}
                jobs={jobs}
                onOpen={(id) => goDocument(library.id, id)}
                onImport={() => setImporting(library)}
                onTasks={() =>
                  setView({ kind: "tasks", libraryId: library.id })
                }
              />
            )}
            {view.tab === "search" && (
              <LibrarySearch
                library={library}
                onOpen={(id) => goDocument(library.id, id)}
              />
            )}
            {view.tab === "projects" && (
              <LibraryProjects
                library={library}
                projectNames={projects.map((p) => ({ id: p.id, name: p.name }))}
                onBind={() => setBinding(library)}
              />
            )}
            {view.tab === "settings" && (
              <LibrarySettings
                library={library}
                onRefresh={refresh}
                onModels={modelPage}
              />
            )}
          </section>
        </>
      )}
      {create && (
        <WorkspaceDialog
          title="新建资料库"
          onClose={() => {
            if (!creating) setCreate(false);
          }}
        >
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void createLibrary();
            }}
          >
            <label>
              资料库名称
              <input
                autoFocus
                required
                maxLength={80}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            <p>创建后可导入文件。检索模型可在资料库设置中配置。</p>
            <Button
              variant="primary"
              type="submit"
              disabled={creating || !name.trim()}
            >
              {creating ? "正在创建…" : "创建资料库"}
            </Button>
          </form>
        </WorkspaceDialog>
      )}
      {binding && (
        <BindProjectDialog
          library={binding}
          onClose={() => setBinding(null)}
          onChanged={refresh}
        />
      )}
      {importing && (
        <ImportWizard
          library={importing}
          onClose={() => setImporting(null)}
          onStarted={() => {
            const id = importing.id;
            setImporting(null);
            void refresh();
            setView({ kind: "tasks", libraryId: id });
          }}
          onModels={modelPage}
        />
      )}
    </div>
  );
}

function DocumentList({
  library,
  jobs,
  onOpen,
  onImport,
  onTasks,
}: {
  library: RagLibrary;
  jobs: RagJob[];
  onOpen: (id: string) => void;
  onImport: () => void;
  onTasks: () => void;
}) {
  const saved = detailCache.get(library.id);
  const [query, setQuery] = useState(saved?.query ?? "");
  const [withdrawn, setWithdrawn] = useState(saved?.withdrawn ?? false);
  const [documents, setDocuments] = useState<RagDocument[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const active = jobs.filter(
    (job) => job.library_id === library.id && !job.parent_id && activeTask(job),
  );
  useEffect(() => {
    let alive = true;
    detailCache.set(library.id, { query, withdrawn });
    const timer = window.setTimeout(() => {
      void ragRepository
        .documents(library.id, 0, query, withdrawn)
        .then((result) => {
          if (alive) {
            setDocuments(result.documents);
            setTotal(result.total);
            setLoaded(true);
            setError("");
          }
        })
        .catch((e) => {
          if (alive) setError(String(e));
        });
    }, 150);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [library.id, library.generation, query, withdrawn, jobs]);
  return (
    <>
      <div className="rag-heading">
        <label>
          查找文档
          <input value={query} onChange={(e) => setQuery(e.target.value)} />
        </label>
        <label className="rag-check">
          <input
            type="checkbox"
            checked={withdrawn}
            onChange={(e) => setWithdrawn(e.target.checked)}
          />
          显示已撤回资料
        </label>
      </div>
      {error && <p role="alert">{error}</p>}
      {active.map((job) => (
        <article className="rag-task-row" key={job.id}>
          <div>
            <strong>{job.filename || "文件导入"}</strong>
            <TaskProgress job={job} />
          </div>
          <Button onClick={onTasks}>查看处理进度</Button>
        </article>
      ))}
      {!loaded && !error && <p role="status">正在读取文档…</p>}
      {loaded && !documents.length && (
        <div className="rag-empty">
          <h3>这里还没有资料</h3>
          <p>导入 PDF、Office、WPS 或文本文件，应用会自动解析并准备检索。</p>
          <Button onClick={onImport}>导入文件</Button>
        </div>
      )}
      {documents.length > 0 && (
        <div className="rag-document-table">
          <div className="rag-document-row rag-document-header">
            <span>文件</span>
            <span>处理与识别状态</span>
            <span>操作</span>
          </div>
          {documents.map((doc) => (
            <article className="rag-document-row" key={doc.id}>
              <div>
                <strong>{doc.original_name || doc.title}</strong>
                <p>
                  {doc.revision || "版本未标注"} ·{" "}
                  {doc.human_reviewed ? "人工已复核" : "机器解析 · 待专业复核"}
                </p>
              </div>
              <div>
                <p>
                  {doc.status === "withdrawn"
                    ? "已撤回"
                    : doc.adopted
                      ? "可检索"
                      : doc.processing_stage === "initial"
                        ? "处理中预览"
                        : "等待索引或处理"}{" "}
                  · {doc.chunk_count} 片段
                </p>
                <p>
                  {doc.ocr_summary?.coverage === "complete"
                    ? `OCR 完成 ${doc.ocr_summary.ocr_pages ?? 0} 页`
                    : doc.ocr_summary?.coverage === "partial"
                      ? "识别范围不完整"
                      : "原生文字"}
                </p>
              </div>
              <Button onClick={() => onOpen(doc.version_id)}>预览</Button>
            </article>
          ))}
        </div>
      )}
      {documents.length < total && (
        <Button
          onClick={() =>
            void ragRepository
              .documents(library.id, documents.length, query, withdrawn)
              .then((result) =>
                setDocuments([...documents, ...result.documents]),
              )
              .catch((e) => setError(String(e)))
          }
        >
          加载更多文档
        </Button>
      )}
    </>
  );
}
function LibrarySearch({
  library,
  onOpen,
}: {
  library: RagLibrary;
  onOpen: (id: string) => void;
}) {
  const [query, setQuery] = useState(searchCache.get(library.id)?.query ?? "");
  const [items, setItems] = useState<RagEvidence[]>(
    searchCache.get(library.id)?.items ?? [],
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    searchCache.set(library.id, { query, items });
  }, [library.id, query, items]);
  return (
    <>
      <h3>检索测试</h3>
      <p>
        直接验证本资料库的条款检索结果。项目对话按项目已启用的资料范围执行。
      </p>
      {!library.active_index_id && (
        <p>暂无就绪索引。请先导入资料并完成处理。</p>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (busy || !query.trim()) return;
          setBusy(true);
          setError("");
          void ragRepository
            .searchLibrary(library.id, query)
            .then((result) => setItems(result.evidence))
            .catch((e) => setError(String(e)))
            .finally(() => setBusy(false));
        }}
      >
        <label>
          检索问题
          <input value={query} onChange={(e) => setQuery(e.target.value)} />
        </label>
        <Button
          type="submit"
          disabled={busy || !query.trim() || !library.active_index_id}
        >
          {busy ? "正在检索…" : "检索条款"}
        </Button>
      </form>
      {error && <p role="alert">{error}</p>}
      {items.map((item, index) => (
        <article className="rag-search-result" key={item.id ?? index}>
          <strong>
            {item.title} · {item.revision}
          </strong>
          <p>
            {typeof item.locator === "object"
              ? `第 ${item.locator.page ?? "—"} 页`
              : item.locator}
          </p>
          <pre>{item.text}</pre>
          <Button onClick={() => onOpen(item.version_id)}>打开来源文档</Button>
        </article>
      ))}
    </>
  );
}
function LibraryProjects({
  library,
  projectNames,
  onBind,
}: {
  library: RagLibrary;
  projectNames: { id: string; name: string }[];
  onBind: () => void;
}) {
  const [names, setNames] = useState<string[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    void Promise.all(
      projectNames.map(async (project) => ({
        name: project.name,
        bound: (await ragRepository.listLibraries(project.id)).libraries.some(
          (item) => item.id === library.id && item.enabled,
        ),
      })),
    )
      .then((rows) => {
        if (alive)
          setNames(rows.filter((row) => row.bound).map((row) => row.name));
      })
      .catch((e) => {
        if (alive) setError(String(e));
      });
    return () => {
      alive = false;
    };
  }, [library.id, library.project_count]);
  return (
    <>
      <h3>使用此资料库的项目</h3>
      <p>已启用的项目自动采用就绪更新。</p>
      {error && <p role="alert">{error}</p>}
      {names.length ? (
        names.map((name) => <p key={name}>{name} · 已启用</p>)
      ) : (
        <p>尚未用于项目。</p>
      )}
      <Button onClick={onBind}>添加到项目</Button>
    </>
  );
}
function LibrarySettings({
  library,
  onRefresh,
  onModels,
}: {
  library: RagLibrary;
  onRefresh: () => Promise<void>;
  onModels: (tab: "retrieval" | "ocr") => void;
}) {
  const [models, setModels] = useState<RagModelList | null>(null);
  const [full, setFull] = useState<RagLibrary | null>(null);
  const [embedding, setEmbedding] = useState(library.embedding_model_id);
  const [ranking, setRanking] = useState(library.reranker_model_id ?? "");
  const [mode, setMode] = useState<"auto" | "off">(library.ocr_mode ?? "auto");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  useEffect(() => {
    let alive = true;
    void ragRepository
      .listModels()
      .then((result) => {
        if (alive) setModels(result);
      })
      .catch((e) => {
        if (alive) setError(String(e));
      });
    return () => {
      alive = false;
    };
  }, [library.id]);
  useEffect(() => {
    if (!advanced) return;
    let alive = true;
    void ragRepository
      .listLibraries()
      .then((result) => {
        if (alive)
          setFull(
            result.libraries.find((item) => item.id === library.id) ?? null,
          );
      })
      .catch((e) => {
        if (alive) setError(String(e));
      });
    return () => {
      alive = false;
    };
  }, [advanced, library.id, library.generation]);
  async function save() {
    setBusy(true);
    setError("");
    try {
      await ragRepository.updateLibrary(library.id, {
        name: library.name,
        embedding_model_id: embedding,
        reranker_model_id: ranking,
        ocr_mode: mode,
      });
      await onRefresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <h3>资料库设置</h3>
      {error && <p role="alert">{error}</p>}
      <label>
        嵌入模型
        <select
          value={embedding}
          onChange={(e) => setEmbedding(e.target.value)}
        >
          <option value="">选择检索模型</option>
          {models?.models
            .filter((m) => m.role === "embedding")
            .map((m) => (
              <option key={m.id} value={m.id}>
                {m.name} · {m.device} · {m.status}
              </option>
            ))}
        </select>
      </label>
      <label>
        重排序模型
        <select value={ranking} onChange={(e) => setRanking(e.target.value)}>
          <option value="">选择排序模型</option>
          {models?.models
            .filter((m) => m.role === "reranker")
            .map((m) => (
              <option key={m.id} value={m.id}>
                {m.name} · {m.device} · {m.status}
              </option>
            ))}
        </select>
      </label>
      <Button onClick={() => onModels("retrieval")}>管理检索模型</Button>
      <label>
        默认 OCR 模式
        <select
          value={mode}
          onChange={(e) => setMode(e.target.value as "auto" | "off")}
        >
          <option value="auto">自动补充图片文字与表格</option>
          <option value="off">仅提取已有文字</option>
        </select>
      </label>
      <Button onClick={() => onModels("ocr")}>管理 OCR 模型</Button>
      <Button
        variant="primary"
        disabled={busy || !embedding || !ranking}
        onClick={() => void save()}
      >
        保存资料库设置
      </Button>
      {(!embedding || !ranking) && (
        <p>请选择嵌入和排序模型，或前往模型管理导入。</p>
      )}
      <details
        open={advanced}
        onToggle={(e) => setAdvanced(e.currentTarget.open)}
      >
        <summary>高级维护</summary>
        <p>
          更换检索模型或手动调整版本后，可重建索引。当前问答继续使用上一就绪索引。
        </p>
        <p>上次索引发布时间 {dateLabel(library.indexed_at)}</p>
        {full && (
          <IndexVersionPicker
            library={full}
            busy={busy}
            onBuild={(ids) => {
              setBusy(true);
              void ragRepository
                .buildIndex(library.id, ids)
                .then(onRefresh)
                .catch((e) => setError(String(e)))
                .finally(() => setBusy(false));
            }}
          />
        )}
        <details>
          <summary>技术信息</summary>
          <code>{library.active_index_id || "暂无索引"}</code>
        </details>
      </details>
    </>
  );
}
