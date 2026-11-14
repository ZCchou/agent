import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Activity, RotateCcw, Settings, X } from "lucide-react";
import { listen } from "@tauri-apps/api/event";
import type { ModelRunEvent } from "../../types/domain";
import { Button } from "../../components/ui/Button";
import { acceptExecutionSnapshot, controlExecutionTask, getExecutionTask,
  type ExecutionOwner, type ExecutionTaskSnapshot } from "../../services/tauri/executionTasks";

export function ExecutionTaskCard({ owner, enabled, onSettings, onRecovered, onRunRetry }: {
  owner: ExecutionOwner | null; enabled: boolean; onSettings(): void;
  onRecovered?(): void; onRunRetry?(runId: string): Promise<void>;
}) {
  const { t } = useTranslation("chat");
  const [task, setTask] = useState<ExecutionTaskSnapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pollEpoch, setPollEpoch] = useState(0);
  const ownerKey = owner ? `${owner.kind}:${owner.id}` : "";
  const currentOwnerKey = useRef(ownerKey);
  currentOwnerKey.current = ownerKey;
  useEffect(() => {
    setTask(null); setError(""); setBusy(false);
    if (!enabled || !owner || !("__TAURI_INTERNALS__" in window)) return;
    let disposed = false;
    let latest: ExecutionTaskSnapshot | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let unlisten: (() => void) | undefined;
    const currentOwner = owner;
    function accept(next: ExecutionTaskSnapshot) {
      if (disposed || !acceptExecutionSnapshot(latest, next, currentOwner)) return false;
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
  if (!enabled || !owner || !task || !acceptExecutionSnapshot(null, task, owner)
    || task.status === "completed" || task.status === "cancelled") return null;
  const details = task.current_stage?.details ?? {};
  const completed = task.current_stage?.completed;
  const total = task.current_stage?.total;
  const paused = task.status === "waiting_recovery" || task.status === "interrupted" || task.status === "failed";
  const stage = typeof details.stage === "string" ? details.stage
    : typeof details.phase === "string" ? details.phase : task.stage ?? "preparingRequest";
  const label = t(`execution.stages.${stage}`, {
    defaultValue: t(`routing.status.${stage}`, { defaultValue: t("execution.stages.processing") }),
  });
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
      {typeof details.model_name === "string" ? <span>{details.model_name}</span> : null}
    </div>
    <div role="status" className="execution-task-card__progress">
      {typeof completed === "number" && typeof total === "number" && total > 0
        ? <span>{t("execution.count", { completed, total })}</span> : null}
      {typeof task.elapsed_seconds === "number" ? <span>{t("execution.elapsed", { seconds: Math.floor(task.elapsed_seconds) })}</span> : null}
      {typeof task.seconds_since_progress === "number" ? <span>{t("execution.lastProgress", { seconds: Math.floor(task.seconds_since_progress) })}</span> : null}
    </div>
    {task.status === "running" && (task.elapsed_seconds ?? 0) >= (task.policy?.long_running_notice_seconds ?? 90)
      ? <p>{t("execution.takingLonger")}</p> : null}
    {paused ? <p>{t("execution.retained")}</p> : null}
    <details><summary>{t("execution.details")}</summary>
      <p>{t("execution.recoveries", { count: task.auto_recoveries })}</p>
      {task.error_code ? <p>{t(`execution.errors.${task.error_code}`, { defaultValue: task.error_code })}</p> : null}
      {typeof details.reason === "string" ? <p>{details.reason}</p> : null}
    </details>
    <div className="execution-task-card__actions">
      {paused && task.can_continue !== false ? <Button busy={busy} icon={<Activity />} onClick={() => void act("continue")}>{t("execution.continue")}</Button> : null}
      {paused && task.can_retry !== false ? <Button busy={busy} icon={<RotateCcw />} onClick={() => void act("retry")}>{t("execution.retry")}</Button> : null}
      {paused && task.recovery_action === "run_retry" && onRunRetry ? <Button busy={busy} icon={<RotateCcw />} onClick={() => void act("run_retry")}>{t("execution.retryRun")}</Button> : null}
      {paused || task.status === "failed" ? <Button disabled={busy} icon={<Settings />} onClick={onSettings}>{t("execution.settings")}</Button> : null}
      {task.can_cancel !== false && task.status !== "failed" ? <Button disabled={busy} icon={<X />} onClick={() => void act("cancel")}>{t("execution.cancel")}</Button> : null}
    </div>
    {error ? <p role="alert">{error}</p> : null}
  </section>;
}
