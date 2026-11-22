import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { modelPhaseTiming, retrievalStage } from "./modelPhaseTiming";
import { DocumentProgressPanel } from "./DocumentProgressPanel";
import { documentProcessingLabel } from "./documentProcessing";
import { Activity, RotateCcw, Settings, X } from "lucide-react";
import { listen } from "@tauri-apps/api/event";
import type { ModelRunEvent } from "../../types/domain";
import { Button } from "../../components/ui/Button";
import { IntentTaskDialog } from "./IntentTaskDialog";
import type { IntentTask } from "../../services/tauri/intentTasks";
import { acceptExecutionSnapshot, retainDocumentProgress, controlExecutionTask, getExecutionTask, getTaskItems, respondTaskQuestion,
  type ExecutionOwner, type ExecutionTaskSnapshot, type TaskItemsPage } from "../../services/tauri/executionTasks";

export function ExecutionTaskCard({ owner, enabled, onSettings, onRecovered, onRunRetry }: {
  owner: ExecutionOwner | null; enabled: boolean; onSettings(): void;
  onRecovered?(): void; onRunRetry?(runId: string): Promise<void>;
}) {
  const { t } = useTranslation("chat");
  const [task, setTask] = useState<ExecutionTaskSnapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pollEpoch, setPollEpoch] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const [items, setItems] = useState<TaskItemsPage | null>(null);
  const ownerKey = owner ? `${owner.kind}:${owner.id}` : "";
  const currentOwnerKey = useRef(ownerKey);
  currentOwnerKey.current = ownerKey;
  useEffect(() => {
    setTask(null); setError(""); setBusy(false); setExpanded(false); setItems(null);
    if (!enabled || !owner || !("__TAURI_INTERNALS__" in window)) return;
    let disposed = false;
    let latest: ExecutionTaskSnapshot | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let unlisten: (() => void) | undefined;
    const currentOwner = owner;
    function accept(next: ExecutionTaskSnapshot) {
      if (disposed || !acceptExecutionSnapshot(latest, next, currentOwner)) return false;
      next = retainDocumentProgress(latest, next);
      latest = next;
      setTask((previous) => acceptExecutionSnapshot(previous, next, currentOwner) ? next : previous);
      return true;
    }
    void listen<ModelRunEvent>("model-run-event", ({ payload }) => {
      if (payload.event_type === "execution.activity") accept(payload.data as unknown as ExecutionTaskSnapshot);
    }).then((release) => { if (disposed) release(); else unlisten = release; }).catch(() => undefined);
    async function poll() {
      let terminal = false;
      try {
        const next = await getExecutionTask(currentOwner);
        if (next && accept(next)) {
          terminal = ["completed", "failed", "cancelled"].includes(next.status);
        }
      } catch { /* A short control connection failure leaves the running task available for the next query. */ }
      if (!disposed && !terminal) timer = setTimeout(() => void poll(), 1000);
    }
    void poll();
    return () => { disposed = true; if (timer) clearTimeout(timer); unlisten?.(); };
  }, [owner?.kind, owner?.id, enabled, pollEpoch]);
  useEffect(() => {
    if (!expanded || !task?.id) return;
    let disposed = false;
    void getTaskItems(task.id).then(page => { if (!disposed) setItems(page); })
      .catch(() => { if (!disposed) setItems(null); });
    return () => { disposed = true; };
  }, [expanded, task?.id, task?.coverage?.completed, task?.coverage?.gaps, task?.coverage?.remaining]);
  if (!enabled || !owner || !task || !acceptExecutionSnapshot(null, task, owner)
    ) return null;
  const completedTask = task.status === "completed";
  if (completedTask && !task.document_progress && !task.verification && !(task.coverage?.total) && !(task.plan && task.plan.steps.length > 1)) return null;
  const details = task.current_stage?.details ?? {};
  const completed = task.current_stage?.completed;
  const total = task.current_stage?.total;
  const paused = task.status === "interrupted" || task.status === "failed";
  const retrieval = retrievalStage(task);
  const stage = retrieval ?? (typeof details.stage === "string" ? details.stage
    : typeof details.phase === "string" ? details.phase : task.stage ?? "preparingRequest");
  const progressLabel = documentProcessingLabel({ ...details, phase: stage, document_phase: retrieval ? undefined : details.document_phase, completed, total },
    (key, values) => t(key, values));
  const modelTiming = modelPhaseTiming(task);
  const label = task.status === "cancelled" ? t("tools.taskCancelled") : completedTask ? t((task.coverage?.gaps ?? 0) > 0 ? "execution.completedWithGaps" : "execution.completed")
    : task.pending_question ? t("execution.waitingInput") : modelTiming ? t(`execution.modelPhases.${modelTiming.phase}`) : progressLabel ?? t(`execution.stages.${stage}`, {
    defaultValue: t(`routing.status.${stage}`, { defaultValue: t("execution.stages.processing") }),
  });
  const clarificationTask: IntentTask | undefined = task.pending_question ? {
    id: task.pending_question.id, scope: `execution:${task.id}`, revision: task.pending_question.revision,
    status: "waiting_user", request: {}, attempts: task.attempt, elapsed_seconds: task.elapsed_seconds ?? 0,
    remaining_seconds: null, timeout_seconds: 0, budget_seconds: null, budget_remaining_seconds: null,
    can_extend: false, can_retry: false, clarification: task.pending_question.clarification, error: null, result: null,
    execution_task_id: task.id,
  } : undefined;
  async function act(action: "continue" | "retry" | "cancel" | "run_retry") {
    if (!task || busy) return;
    const taskId = task.id;
    const actionOwner = ownerKey;
    setBusy(true); setError("");
    try {
      if (action === "run_retry") {
        if (task.recovery_run_id && onRunRetry) await onRunRetry(task.recovery_run_id);
      } else {
        const next = await controlExecutionTask(task, action);
        if (currentOwnerKey.current === actionOwner) {
          setTask((current) => current?.id === taskId && owner && acceptExecutionSnapshot(current, next, owner) ? next : current);
          if (action === "retry") { setPollEpoch(value => value + 1); onRecovered?.(); }
        }
      }
    } catch (value) { if (currentOwnerKey.current === actionOwner) setError(value instanceof Error ? value.message : String(value)); }
    finally { if (currentOwnerKey.current === actionOwner) setBusy(false); }
  }
  return <section className="execution-task-card" aria-label={t("execution.title")}>
    <div className="execution-task-card__heading"><Activity size={16} aria-hidden="true" />
      <strong>{paused ? t("execution.needsAttention") : label}</strong>
      {typeof details.device === "string" ? <span>{details.device.toUpperCase()}</span> : null}
      {(!retrieval || details.model_role === "reranker" || details.model_role === "embedding") && typeof details.model_name === "string" ? <span>{details.model_name}</span> : null}
    </div>
    <div role="status" className="execution-task-card__progress">
      {progressLabel && (paused || completedTask || task.status === "cancelled") ? <span>{progressLabel}</span> : null}
      {!progressLabel && typeof completed === "number" && typeof total === "number" && total > 0
        ? <span>{t("execution.count", { completed, total })}</span> : null}
      {typeof task.elapsed_seconds === "number" ? <span>{t("execution.elapsed", { seconds: Math.floor(task.elapsed_seconds) })}</span> : null}
      {modelTiming?.phaseSeconds != null ? <span>{t("execution.phaseTime", { seconds: modelTiming.phaseSeconds })}</span> : null}
      {modelTiming?.timings.map(metric => <span key={metric.phase}>{t(`execution.modelTimings.${metric.phase}`, { seconds: metric.seconds })}</span>)}
      {(!modelTiming || modelTiming.phase === "generating") && typeof task.seconds_since_progress === "number" ? <span>{t("execution.lastProgress", { seconds: Math.floor(task.seconds_since_progress) })}</span> : null}
    </div>
    <DocumentProgressPanel progress={task.document_progress} />
    {task.status === "running" && !task.pending_question && (task.elapsed_seconds ?? 0) >= (task.policy?.long_running_notice_seconds ?? 90)
      ? <p>{t("execution.takingLonger")}</p> : null}
    {paused ? <p>{t("execution.retained")}</p> : null}
    {paused && typeof details.reason === "string" ? <p>{details.reason}</p> : null}
    <details open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}><summary>{t("execution.details")}</summary>
      {task.plan ? <>
        <p>{task.plan.goal}</p>
        <ol>{task.plan.steps.map(step => <li key={step.id}>{step.description}</li>)}</ol>
        {task.plan.source_scopes?.map(source => <p key={`${source.source_id}:${source.operation}`}>
          {task.plan?.request?.attachments?.find(item => item.id === source.source_id)?.name ?? source.source_id}
          {" · "}{t(`execution.scopes.${source.scope}`)}{source.reason ? ` · ${source.reason}` : ""}
          {" · "}{t(`execution.operations.${source.operation}`)}
        </p>)}
        {task.plan.deliverables?.map(output => <p key={output.path}>{t("execution.deliverable", { path: output.path })}</p>)}
      </> : null}
      {task.coverage && task.coverage.total > 0 ? <p>{t("execution.coverage", { ...task.coverage })}</p> : null}
      {task.verification ? <>
        <p>{task.verification.summary}</p>
        {task.verification.unmet_requirements.map((item, index) => <p key={index}>{item}</p>)}
      </> : null}
      {items?.items.filter(item => item.outcome === "gap" || item.outcome === "failed").map(item => <p key={`${item.source_id}:${item.target_id}`}>
        {item.target_id} · {[...(item.result?.gaps ?? []), ...(item.result?.version_issues ?? [])].join(" · ") || item.result?.summary}
        {item.result?.error_code ? ` · ${item.result.error_code}` : ""}
      </p>)}
      {items?.next_offset != null ? <Button onClick={() => {
        const key = currentOwnerKey.current;
        void getTaskItems(task.id, items.next_offset!).then(page => {
          if (currentOwnerKey.current === key) setItems(previous => ({ ...page, items: [...(previous?.items ?? []), ...page.items] }));
        }).catch(value => { if (currentOwnerKey.current === key) setError(String(value)); });
      }}>{t("execution.moreItems")}</Button> : null}
      <p>{t("execution.recoveries", { count: task.auto_recoveries })}</p>
      {task.error_code ? <p>{t(`execution.errors.${task.error_code}`, { defaultValue: task.error_code })}</p> : null}
    </details>
    <div className="execution-task-card__actions">
      {paused && task.can_retry !== false ? <Button busy={busy} icon={<RotateCcw />}
        onClick={() => void act(task.recovery_action === "run_retry" && onRunRetry ? "run_retry" : "retry")}>{t("execution.retry")}</Button> : null}
      {paused && /model_|resource|memory|vram|runtime_|engine_|access_denied|context_too_small/.test(task.error_code ?? "")
        ? <Button disabled={busy} icon={<Settings />} onClick={onSettings}>{t("execution.settings")}</Button> : null}
      {!completedTask && task.status !== "cancelled" && task.can_cancel !== false && task.status !== "failed" ? <Button disabled={busy} icon={<X />} onClick={() => void act("cancel")}>{t("execution.cancel")}</Button> : null}
    </div>
    {error ? <p role="alert">{error}</p> : null}
    {clarificationTask ? <IntentTaskDialog scope={clarificationTask.scope} enabled={enabled}
      externalTask={clarificationTask} onModelSettings={onSettings} onAccepted={async () => false}
      onRespond={async answers => {
        const actionOwner = ownerKey;
        const next = await respondTaskQuestion(task, answers);
        if (currentOwnerKey.current === actionOwner) {
          setTask(current => current && owner && acceptExecutionSnapshot(current, next, owner) ? next : current);
        }
      }} /> : null}
  </section>;
}
