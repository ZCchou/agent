import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";
import { intentTasks } from "../../services/tauri/intentTasks";
import type { AcceptedModelRun } from "../../services/repositories/ModelRunRepository";

type Answer = { choice: string; draft: string; mode: "choice" | "custom" };
const drafts = new Map<string, Record<string, Answer>>();

export function IntentTaskDialog({ scope, enabled, onAccepted, onModelSettings }: {
  scope: string; enabled: boolean;
  onAccepted(result: AcceptedModelRun): Promise<boolean>;
  onModelSettings(): void;
}) {
  const { t } = useTranslation("chat");
  const task = useSyncExternalStore(intentTasks.subscribe, () => intentTasks.get(scope));
  const revision = task ? `${scope}:${task.id}:${task.revision}` : "";
  const [hidden, setHidden] = useState("");
  const [answers, setAnswers] = useState<Record<string, Answer>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const actionLock = useRef(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const adopting = useRef("");
  const clarification = task?.status === "waiting_user" ? task.clarification : null;
  const open = enabled && Boolean(clarification) && hidden !== revision;

  useEffect(() => {
    if (enabled && "__TAURI_INTERNALS__" in window) void intentTasks.restore(scope).catch(() => undefined);
  }, [scope, enabled]);
  useEffect(() => { setAnswers(drafts.get(revision) ?? {}); setError(""); }, [revision]);
  useEffect(() => {
    if (!enabled || !task || task.status !== "completed" || !task.result
      || intentTasks.hasAcceptanceOwner(task.id) || intentTasks.isConsumed(task.id)) return;
    if (task.result.status === "preflight_rejected") { intentTasks.consume(task.id); return; }
    const identity = `${scope}-${task.id}-${task.revision}`;
    if (adopting.current === identity) return;
    adopting.current = identity;
    void onAccepted(task.result).then((accepted) => {
      if (accepted) intentTasks.acknowledge(scope, task.result!);
    }).finally(() => { if (adopting.current === identity) adopting.current = ""; });
  }, [enabled, scope, task, onAccepted]);
  useEffect(() => {
    const element = dialog.current;
    if (open && element && !element.open) element.showModal();
    if (!open && element?.open) element.close();
  }, [open, revision]);

  if (!task || !enabled || ["completed", "cancelled"].includes(task.status)) return null;
  function updateAnswer(id: string, change: Partial<Answer>) {
    setAnswers(previous => {
      const current = previous[id] ?? { choice: "", draft: "", mode: "custom" as const };
      const next = { ...previous, [id]: { ...current, ...change } };
      drafts.set(revision, next);
      return next;
    });
  }
  const valueFor = (id: string) => answers[id]?.mode === "choice" ? answers[id].choice : answers[id]?.draft ?? "";
  const act = async (action: "respond" | "retry") => {
    if (actionLock.current) return;
    actionLock.current = true; setBusy(true); setError("");
    try {
      const values = Object.fromEntries((clarification?.questions ?? []).map(q => [q.id, valueFor(q.id).trim()]));
      await intentTasks.act(scope, action, action === "respond" ? values : undefined);
      if (action === "respond") drafts.delete(revision);
    } catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { actionLock.current = false; setBusy(false); }
  };
  const close = () => { setHidden(revision); dialog.current?.close(); };
  const resourceError = /model_|resource|out_of_memory|insufficient_|runtime_|engine_|access_denied/.test(task.error?.code ?? "");
  if (!clarification) return task.error && !task.execution_task_id ? <section className="execution-task-card" role="status">
    <strong>{t(task.status === "interrupted" ? "intent.interrupted" : "intent.failed")}</strong>
    <p>{task.error.message}</p>
    <details><summary>{t("intent.technicalError")}</summary><p>{task.error.code}</p></details>
    <div className="execution-task-card__actions">
      {task.can_retry ? <Button busy={busy} onClick={() => void act("retry")}>{t("intent.retry")}</Button> : null}
      {resourceError ? <Button disabled={busy} onClick={onModelSettings}>{t("intent.modelSettings")}</Button> : null}
    </div>
    {error ? <p role="alert">{error}</p> : null}
  </section> : null;

  return <>
    {!open ? <Button variant="ghost" onClick={() => setHidden("")}>{t("intent.resumeInput")}</Button> : null}
    <dialog ref={dialog} className="intent-task-dialog" aria-labelledby="intent-task-title"
      aria-describedby="intent-task-summary" onCancel={event => { event.preventDefault(); if (!busy) close(); }}>
      <header><div><h2 id="intent-task-title">{t("intent.title")}</h2>
        <p id="intent-task-summary">{clarification.summary}</p></div>
        <Button variant="ghost" icon={<X />} aria-label={t("intent.close")} disabled={busy} onClick={close} />
      </header>
      <form id="intent-answers" className="intent-task-dialog__body" onSubmit={event => { event.preventDefault(); void act("respond"); }}>
        {clarification.questions.slice(0, 3).map(question => <fieldset key={question.id} disabled={busy}>
          <legend>{question.question}</legend>
          <div className="intent-task-dialog__options">
            {question.options?.map(option => <label key={option} className="intent-task-dialog__option">
              <input type="radio" name={`${revision}:${question.id}`} checked={answers[question.id]?.mode === "choice" && answers[question.id]?.choice === option}
                onChange={() => updateAnswer(question.id, { choice: option, mode: "choice" })} />
              <span>{option}</span>
            </label>)}
          </div>
          <label className="intent-task-dialog__answer">{t("intent.freeText")}
            <textarea rows={3} value={answers[question.id]?.draft ?? ""}
              onFocus={() => { if (answers[question.id]?.draft) updateAnswer(question.id, { mode: "custom" }); }}
              onChange={event => updateAnswer(question.id, { draft: event.target.value, mode: "custom" })} />
          </label>
        </fieldset>)}
        {error ? <p role="alert">{error}</p> : null}
      </form>
      <footer><Button form="intent-answers" type="submit" variant="primary" busy={busy}
        disabled={busy || clarification.questions.some(q => !valueFor(q.id).trim())}>{t("intent.confirm")}</Button></footer>
    </dialog>
  </>;
}
