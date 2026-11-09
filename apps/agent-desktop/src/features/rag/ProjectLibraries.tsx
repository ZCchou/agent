import { useCallback, useEffect, useState } from "react";
import { Button } from "../../components/ui/Button";
import { useApp } from "../../contexts/AppContext";
import { ragRepository } from "../../services/runtime";
import type { RagLibrary } from "../../types/rag";
import { dateLabel, navigateRag, WorkspaceDialog } from "./workspaceShared";

export function BindProjectDialog({
  library,
  onClose,
  onChanged,
}: {
  library: RagLibrary;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const { projects, startNewProject } = useApp();
  const [selected, setSelected] = useState<string[]>([]);
  const [initial, setInitial] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    void Promise.all(
      projects.map(async (project) => ({
        id: project.id,
        libraries: (await ragRepository.listLibraries(project.id)).libraries,
      })),
    )
      .then((rows) => {
        if (!alive) return;
        const bound = rows
          .filter((row) =>
            row.libraries.some(
              (item) => item.id === library.id && item.enabled,
            ),
          )
          .map((row) => row.id);
        setInitial(bound);
        setSelected(bound);
        setLoading(false);
      })
      .catch((e) => {
        if (alive) {
          setError(String(e));
          setLoading(false);
        }
      });
    return () => {
      alive = false;
    };
  }, [library.id, projects]);
  async function apply() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      for (const project of projects) {
        if (initial.includes(project.id) !== selected.includes(project.id))
          await ragRepository.bindLibrary(
            library.id,
            project.id,
            selected.includes(project.id),
          );
      }
      await onChanged();
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <WorkspaceDialog
      title="添加到项目"
      onClose={() => {
        if (!busy) onClose();
      }}
    >
      <p>
        资料库 <strong>{library.name}</strong> · {library.ready_count ?? 0}{" "}
        份文档可检索
      </p>
      <p>启用后，项目自动采用后续处理完成的就绪资料。</p>
      {error && <p role="alert">{error}</p>}
      {!projects.length ? (
        <>
          <p>请先创建一个项目。</p>
          <Button onClick={startNewProject}>创建项目</Button>
        </>
      ) : loading ? (
        <p role="status">正在读取项目使用范围…</p>
      ) : (
        projects.map((project) => (
          <label className="rag-check" key={project.id}>
            <input
              type="checkbox"
              disabled={busy || !!error}
              checked={selected.includes(project.id)}
              onChange={(e) =>
                setSelected(
                  e.target.checked
                    ? [...selected, project.id]
                    : selected.filter((id) => id !== project.id),
                )
              }
            />
            {project.name}
          </label>
        ))
      )}
      {!library.active_index_id && (
        <p>资料库尚无就绪索引。项目可先启用，资料完成处理后自动接入。</p>
      )}
      <Button
        variant="primary"
        disabled={busy || loading || !projects.length || !!error}
        onClick={() => void apply()}
      >
        {busy ? "正在应用…" : "应用到所选项目"}
      </Button>
    </WorkspaceDialog>
  );
}

export function ProjectLibraries({
  projectId,
  compact = false,
}: {
  projectId: string;
  compact?: boolean;
}) {
  const { setCurrentPage } = useApp();
  const [all, setAll] = useState<RagLibrary[]>([]);
  const [bindings, setBindings] = useState<RagLibrary[]>([]);
  const [open, setOpen] = useState(false);
  const [selection, setSelection] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const load = useCallback(async () => {
    const [catalog, bound] = await Promise.all([
      ragRepository.overview(),
      ragRepository.listLibraries(projectId),
    ]);
    setAll(catalog.libraries);
    setBindings(bound.libraries.filter((item) => item.enabled));
    setLoaded(true);
  }, [projectId]);
  useEffect(() => {
    let alive = true;
    setLoaded(false);
    setError("");
    void Promise.all([
      ragRepository.overview(),
      ragRepository.listLibraries(projectId),
    ])
      .then(([catalog, bound]) => {
        if (alive) {
          setAll(catalog.libraries);
          setBindings(bound.libraries.filter((item) => item.enabled));
          setLoaded(true);
        }
      })
      .catch((e) => {
        if (alive) setError(String(e));
      });
    const timer = window.setInterval(() => {
      if (alive) void load().catch((e) => setError(String(e)));
    }, 10000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [projectId, load]);
  function choose() {
    setSelection(bindings.map((item) => item.id));
    setOpen(true);
  }
  async function apply() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      for (const library of all) {
        const enabled = bindings.some((item) => item.id === library.id);
        if (enabled !== selection.includes(library.id))
          await ragRepository.bindLibrary(
            library.id,
            projectId,
            selection.includes(library.id),
          );
      }
      await load();
      setOpen(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  const notReady = bindings.some((item) => !item.index_id);
  const updating = all.some(
    (library) =>
      library.processing_count &&
      bindings.some((item) => item.id === library.id),
  );
  const needsAttention = all.some(library => library.attention_count && bindings.some(item => item.id === library.id));
  const label = !loaded
    ? "读取资料范围…"
    : !bindings.length
      ? "添加项目资料"
      : notReady && needsAttention
        ? "资料需要处理 · 暂无可用索引"
        : notReady
        ? "资料处理中 · 等待可用索引"
        : needsAttention
          ? "更新需要处理 · 使用上一就绪资料"
          : updating
          ? `资料正在更新 · ${bindings.length} 个资料库`
          : `资料已启用 · ${bindings.length} 个资料库`;
  const dialog = open && (
    <WorkspaceDialog
      title="本项目使用的资料库"
      onClose={() => {
        if (!busy) setOpen(false);
      }}
    >
      <p>可同时启用多个资料库。就绪资料更新后自动用于后续问答。</p>
      {error && <p role="alert">{error}</p>}
      {all.length ? (
        all.map((library) => (
          <label className="rag-check" key={library.id}>
            <input
              type="checkbox"
              checked={selection.includes(library.id)}
              disabled={busy}
              onChange={(e) =>
                setSelection(
                  e.target.checked
                    ? [...selection, library.id]
                    : selection.filter((id) => id !== library.id),
                )
              }
            />
            {library.name} · {library.ready_count ?? 0} 份可检索
          </label>
        ))
      ) : (
        <p>暂无资料库。请先创建并导入资料。</p>
      )}
      <div className="rag-actions">
        <Button
          variant="primary"
          disabled={busy || !all.length}
          onClick={() => void apply()}
        >
          保存项目资料设置
        </Button>
        <Button onClick={() => navigateRag({ kind: "home" }, setCurrentPage)}>
          管理资料库
        </Button>
      </div>
    </WorkspaceDialog>
  );
  if (compact)
    return (
      <>
        <Button disabled={!loaded && !error} onClick={choose}>
          {error ? "资料范围读取失败，点击重试" : label}
        </Button>
        {dialog}
      </>
    );
  return (
    <section className="project-rag-panel">
      <div className="rag-heading">
        <div>
          <h3>资料与 RAG</h3>
          <p>{label}</p>
        </div>
        <Button disabled={!loaded} onClick={choose}>
          添加资料库
        </Button>
      </div>
      {error && <p role="alert">{error}</p>}
      {bindings.map((binding) => {
        const library = all.find((item) => item.id === binding.id) ?? binding;
        return (
          <article className="rag-project-library" key={binding.id}>
            <div>
              <strong>{library.name}</strong>
              <p>
                {library.ready_count ?? 0} 份文档可检索 · 更新于{" "}
                {dateLabel(library.indexed_at)}
              </p>
              {library.processing_count ? (
                <p>正在更新，当前采用上一就绪索引。</p>
              ) : !binding.index_id ? (
                <p>尚无就绪资料，处理完成后自动接入。</p>
              ) : (
                <p>已启用 · 自动采用就绪版本</p>
              )}
            </div>
            <div className="rag-actions">
              <Button
                onClick={() =>
                  navigateRag(
                    { kind: "library", id: library.id, tab: "documents" },
                    setCurrentPage,
                  )
                }
              >
                查看资料库
              </Button>
              <Button
                onClick={() =>
                  navigateRag(
                    { kind: "library", id: library.id, tab: "search" },
                    setCurrentPage,
                  )
                }
              >
                检索测试
              </Button>
              <Button
                disabled={busy}
                onClick={() => {
                  setBusy(true);
                  void ragRepository
                    .bindLibrary(library.id, projectId, false)
                    .then(load)
                    .catch((e) => setError(String(e)))
                    .finally(() => setBusy(false));
                }}
              >
                移出项目
              </Button>
            </div>
          </article>
        );
      })}
      {!bindings.length && (
        <p>启用资料库后，本项目对话会自动检索其中的条款。</p>
      )}
      {dialog}
    </section>
  );
}
