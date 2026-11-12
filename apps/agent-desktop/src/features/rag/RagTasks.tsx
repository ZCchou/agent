import { useEffect, useRef, useState } from "react";
import { Button } from "../../components/ui/Button";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { ragRepository } from "../../services/runtime";
import type { RagJob, RagLibrary, RagTaskDetail } from "../../types/rag";
import {
  activeTask,
  dateLabel,
  phases,
  sources,
  statuses,
  WorkspaceDialog,
} from "./workspaceShared";

const steps = ["validating", "parsing", "ocr", "indexing", "completed"];
function taskName(job: RagJob) {
  return job.kind === "import.batch"
    ? `导入 ${job.filename || "文件"}`
    : job.kind === "index.build"
      ? "重建检索索引"
      : `导入 ${job.filename || "历史文档"}`;
}
export function TaskProgress({ job }: { job: RagJob }) {
  const phase = job.phase ?? job.status;
  const amount =
    job.total_units != null
      ? `${job.completed_units ?? 0}/${job.total_units}`
      : "";
  return (
    <div className="rag-task-progress" aria-live="polite">
      <strong>
        {job.resolved_at
          ? "已解决 · 历史记录"
          : phase === "needs_attention"
            ? "需要处理"
            : activeTask(job)
              ? (phases[phase] ?? phase)
              : (statuses[job.status] ?? job.status)}
      </strong>
      {job.status === "completed" ? (
        <span>资料处理完成</span>
      ) : amount ? (
        <>
          <span>
            {phase === "ocr"
              ? "已识别"
              : phase === "indexing"
                ? "已向量化"
                : "已处理"}{" "}
            {amount} {phase === "ocr" ? "页" : "片段"}
          </span>
          <progress
            aria-label={phases[phase] ?? phase}
            max={Math.max(1, job.total_units!)}
            value={job.completed_units ?? 0}
          />
        </>
      ) : activeTask(job) ? (
        <progress aria-label={phases[phase] ?? phase} />
      ) : null}
    </div>
  );
}
export function TaskCenter({
  jobs,
  libraries,
  libraryId,
  onRefresh,
  onOpenDocument,
  onModels,
}: {
  jobs: RagJob[];
  libraries: RagLibrary[];
  libraryId?: string;
  onRefresh: () => Promise<void>;
  onOpenDocument: (libraryId: string, versionId: string) => void;
  onModels: (kind: "retrieval" | "ocr") => void;
}) {
  const [detailId, setDetailId] = useState<string | null>(null);
  const visible = jobs.filter(
    (job) => !job.parent_id && (!libraryId || job.library_id === libraryId),
  );
  const current = visible.filter(
    (job) =>
      activeTask(job) ||
      (!job.resolved_at &&
        (job.status === "interrupted" || job.status === "failed")),
  );
  const history = visible.filter((job) => !current.includes(job));
  function row(job: RagJob) {
    return (
      <article key={job.id} className="rag-task-row">
        <div>
          <h3>{taskName(job)}</h3>
          <p>
            资料库{" "}
            {libraries.find((item) => item.id === job.library_id)?.name ?? "—"}{" "}
            · {dateLabel(job.created_at)}
          </p>
          <TaskProgress job={job} />
        </div>
        <Button onClick={() => setDetailId(job.id)}>查看详情</Button>
      </article>
    );
  }
  return (
    <section>
      <h2>处理任务</h2>
      <p>一次导入显示为一个任务，展开后查看文件和各处理阶段。</p>
      {current.length ? current.map(row) : <p>当前没有等待处理的任务。</p>}
      <details>
        <summary>历史任务（{history.length}）</summary>
        {history.map(row)}
      </details>
      {detailId && (
        <TaskDetail
          jobId={detailId}
          libraryName={
            libraries.find(
              (item) =>
                item.id === jobs.find((job) => job.id === detailId)?.library_id,
            )?.name ?? "资料库"
          }
          onClose={() => setDetailId(null)}
          onRefresh={onRefresh}
          onOpenDocument={onOpenDocument}
          onModels={onModels}
        />
      )}
    </section>
  );
}
function TaskDetail({
  jobId,
  libraryName,
  onClose,
  onRefresh,
  onOpenDocument,
  onModels,
}: {
  jobId: string;
  libraryName: string;
  onClose: () => void;
  onRefresh: () => Promise<void>;
  onOpenDocument: (libraryId: string, versionId: string) => void;
  onModels: (kind: "retrieval" | "ocr") => void;
}) {
  const [detail, setDetail] = useState<RagTaskDetail | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmation, setConfirmation] = useState<"cancel" | "accept" | null>(
    null,
  );
  const [now, setNow] = useState(Date.now());
  const samples = useRef<{
    phase: string;
    values: { count: number; time: number }[];
  }>({ phase: "", values: [] });
  async function load() {
    setDetail(await ragRepository.taskDetail(jobId));
  }
  useEffect(() => {
    let alive = true;
    const refresh = () =>
      ragRepository
        .taskDetail(jobId)
        .then((data) => {
          if (alive) setDetail(data);
        })
        .catch((reason) => {
          if (alive) setError(String(reason));
        });
    void refresh();
    const timer = window.setInterval(() => {
      setNow(Date.now());
      void refresh();
    }, 2000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [jobId]);
  async function act(operation: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await operation();
      await load();
      await onRefresh();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }
  const job = detail?.job;
  const phase = job?.phase ?? "";
  if (samples.current.phase !== phase) samples.current = { phase, values: [] };
  if (job?.updated_at && activeTask(job)) {
    const count = job.completed_units ?? 0;
    const last = samples.current.values[samples.current.values.length - 1];
    if (!last || last.count !== count)
      samples.current.values = [
        ...samples.current.values,
        { count, time: new Date(job.updated_at).getTime() },
      ].slice(-8);
  }
  const observations = samples.current.values;
  const first = observations[0],
    last = observations[observations.length - 1];
  const rate =
    first && last && observations.length >= 3 && last.time - first.time >= 4000
      ? (last.count - first.count) / ((last.time - first.time) / 1000)
      : 0;
  const remaining =
    rate > 0 && job?.total_units != null
      ? Math.max(
          0,
          Math.ceil((job.total_units - (job.completed_units ?? 0)) / rate),
        )
      : null;
  const versions =
    detail?.children.filter((child) => child.last_version_id) ?? [];
  return (
    <WorkspaceDialog title="任务详情" onClose={onClose}>
      {error && <p role="alert">{error}</p>}
      {job ? (
        <>
          <h3>{taskName(job)}</h3>
          <p>资料库 {libraryName}</p>
          <TaskProgress job={job} />
          {remaining !== null && activeTask(job) && (
            <p>根据本阶段实测速度，预计还需约 {remaining} 秒</p>
          )}
          <ol className="rag-steps">
            {steps.map((phase) => (
              <li
                key={phase}
                aria-current={job.phase === phase ? "step" : undefined}
              >
                {phases[phase]}
              </li>
            ))}
          </ol>
          <p>
            已耗时{" "}
            {Math.max(
              0,
              Math.round(
                ((job.finished_at ? new Date(job.finished_at).getTime() : now) -
                  new Date(job.started_at ?? job.created_at ?? now).getTime()) /
                  1000,
              ),
            )}{" "}
            秒 · 第 {job.attempt ?? 1} 次尝试
          </p>
          {job.error && (
            <div role="alert">
              <p>
                {job.error_code === "rag_models_missing"
                  ? "请配置检索模型，然后继续处理。"
                  : job.error_code === "rag_ocr_missing"
                    ? "请配置 OCR 模型包后重试识别。"
                    : job.phase === "needs_attention"
                      ? "部分内容需要核对。可重试失败页面，或核对后接受不完整结果。"
                      : "此任务需要处理，请查看下方记录并重试。"}
              </p>
              <details>
                <summary>错误详情</summary>
                <p>{job.error}</p>
              </details>
            </div>
          )}
          {job.resolved_at && <p>后续处理已完成，该记录保留用于追溯。</p>}
          {!activeTask(job) && job.status !== "completed" && (
            <p>
              终止来源{" "}
              {job.termination_source
                ? (sources[job.termination_source] ?? job.termination_source)
                : "来源未记录"}
            </p>
          )}
          <div className="rag-actions">
            {activeTask(job) ? (
              <>
                <Button
                  disabled={busy}
                  onClick={() => void act(() => ragRepository.pauseJob(job.id))}
                >
                  暂停处理
                </Button>
                <Button
                  disabled={busy}
                  onClick={() => setConfirmation("cancel")}
                >
                  取消任务
                </Button>
              </>
            ) : job.kind === "import.batch" &&
              job.status !== "completed" &&
              !job.resolved_at ? (
              <>
                <Button
                  disabled={busy}
                  onClick={() =>
                    void act(() => ragRepository.resumeJob(job.id))
                  }
                >
                  {job.status === "cancelled"
                    ? "重新处理"
                    : job.phase === "needs_attention"
                      ? "重试失败阶段"
                      : "继续处理"}
                </Button>
                {job.phase === "needs_attention" && (
                  <>
                    {(job.error_code === "rag_models_missing" ||
                      job.error_code === "rag_ocr_missing") && (
                      <Button
                        onClick={() =>
                          onModels(
                            job.error_code === "rag_ocr_missing"
                              ? "ocr"
                              : "retrieval",
                          )
                        }
                      >
                        {job.error_code === "rag_ocr_missing"
                          ? "配置 OCR 模型"
                          : "配置检索模型"}
                      </Button>
                    )}
                    {job.error_code !== "rag_models_missing" && (
                      <Button
                        disabled={busy}
                        onClick={() => setConfirmation("accept")}
                      >
                        处理不完整资料
                      </Button>
                    )}
                  </>
                )}
              </>
            ) : job.kind === "index.build" && job.status !== "completed" ? (
              <Button
                disabled={busy}
                onClick={() =>
                  void act(() =>
                    ragRepository.buildIndex(
                      job.library_id,
                      job.payload?.version_ids as string[] | undefined,
                    ),
                  )
                }
              >
                重新建立索引
              </Button>
            ) : null}
          </div>
          <h3>文件与处理阶段</h3>
          {detail.children.map((child) => (
            <article className="rag-task-row" key={child.id}>
              <div>
                <strong>{child.filename || "建立最终索引"}</strong>
                <TaskProgress job={child} />
                {!!child.document_pages && (
                  <p>
                    PDF 共 {child.document_pages} 页，已完成 OCR{" "}
                    {child.ocr_completed_pages ?? 0} 页
                  </p>
                )}
              </div>
              {child.last_version_id && (
                <Button
                  onClick={() => {
                    onOpenDocument(job.library_id, child.last_version_id!);
                    onClose();
                  }}
                >
                  查看解析结果
                </Button>
              )}
            </article>
          ))}
          {!detail.children.length && <p>此历史任务没有阶段明细。</p>}
          {job.status === "completed" && versions.length > 0 && (
            <Button
              onClick={() => {
                onOpenDocument(
                  job.library_id,
                  versions[versions.length - 1].last_version_id!,
                );
                onClose();
              }}
            >
              查看文档
            </Button>
          )}
          <details>
            <summary>操作记录与技术信息</summary>
            {detail.events.map((event) => (
              <p key={event.id}>
                {dateLabel(event.created_at)} · {event.action} ·{" "}
                {sources[event.source] ?? event.source} ·{" "}
                {phases[event.phase] ?? event.phase}
              </p>
            ))}
            <code>{job.id}</code>
          </details>
          <ConfirmDialog
            open={confirmation !== null}
            title={
              confirmation === "cancel"
                ? "取消本次导入？"
                : "接受不完整识别结果？"
            }
            description={
              confirmation === "cancel"
                ? `将停止 ${job.filename || "该文件"} 在资料库“${libraryName}”中的处理。`
                : "请先核对缺失页面。接受后，当前识别到的内容将建立索引，并保留覆盖缺口标记。"
            }
            confirmLabel={
              confirmation === "cancel" ? "确认取消" : "已核对，接受不完整结果"
            }
            busy={busy}
            onCancel={() => setConfirmation(null)}
            onConfirm={() => {
              const operation =
                confirmation === "cancel"
                  ? () => ragRepository.cancelJob(job.id)
                  : () => ragRepository.resumeJob(job.id, true);
              setConfirmation(null);
              void act(operation);
            }}
          />
        </>
      ) : (
        <p role="status">读取任务详情…</p>
      )}
    </WorkspaceDialog>
  );
}
