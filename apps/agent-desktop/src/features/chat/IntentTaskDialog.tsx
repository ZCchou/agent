import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";
import { intentTasks } from "../../services/tauri/intentTasks";
import type { AcceptedModelRun } from "../../services/repositories/ModelRunRepository";

export function IntentTaskDialog({ scope, enabled, onAccepted, onModelSettings }: {
  scope: string;
  enabled: boolean;
  onAccepted(result: AcceptedModelRun): Promise<boolean>;
  onModelSettings(): void;
}) {
  const { t } = useTranslation("chat");
  const task = useSyncExternalStore(intentTasks.subscribe, () => intentTasks.get(scope));
  const [hidden, setHidden] = useState("");
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const adopting = useRef("");
  const revision = task ? `${task.id}-${task.revision}` : "";
  const clarification = task?.status === "waiting_user" && task.clarification;
  const notice = Boolean(task && (clarification || task.status === "failed" || task.status === "interrupted" || (task.status === "waiting_user" && !task.clarification)
    || (task.status === "identifying" && task.remaining_seconds <= 15)));
  const open = enabled && notice && hidden !== revision;

  useEffect(() => {
    if (enabled && "__TAURI_INTERNALS__" in window) void intentTasks.restore(scope).catch(() => undefined);
  }, [scope, enabled]);
  useEffect(() => { setAnswers({}); setError(""); }, [revision, scope]);
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
  }, [open]);

  if (!task || !enabled || ["completed", "cancelled"].includes(task.status)) return null;
  const act = async (action: "respond" | "retry" | "extend" | "cancel") => {
    if (busy) return;
    setBusy(true); setError("");
    try { await intentTasks.act(scope, action, action === "respond" ? answers : undefined); }
    catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setBusy(false); }
  };
  const close = () => { setHidden(revision); dialog.current?.close(); };
  const resourceError = /model_(missing|unavailable|not_enabled|not_found)|resource|out_of_memory|insufficient_(memory|vram)|runtime_integrity/.test(task.error?.code ?? "");
  return <>
    {notice && !open ? <Button variant="ghost" onClick={() => setHidden("")}>{t(clarification ? "intent.resumeInput" : "intent.review")}</Button> : null}
    <dialog ref={dialog} className="confirm-dialog intent-task-dialog" aria-labelledby="intent-task-title"
      aria-describedby="intent-task-summary" onCancel={(event) => { event.preventDefault(); if (!busy) close(); }}
      style={{ maxWidth: "min(560px, calc(100vw - 24px))", maxHeight: "calc(100dvh - 32px)", overflowY: "auto", border: "1px solid var(--border-color)", borderRadius: 16 }}>
      <header><div><h2 id="intent-task-title">{t(clarification ? "intent.title" : "intent.timeoutTitle")}</h2>
        <p id="intent-task-summary">{clarification ? clarification.summary : t("intent.timeoutDescription")}</p></div>
        <button type="button" aria-label={t("intent.close")} disabled={busy} onClick={close}>×</button></header>
      {clarification ? <form id="intent-answers" onSubmit={(event) => { event.preventDefault(); void act("respond"); }}>
        {clarification.questions.slice(0, 3).map((question) => <fieldset key={question.id} disabled={busy}>
          <legend>{question.question}</legend>
          {question.options?.map((option) => <label key={option} style={{ display: "block" }}><input type="radio" name={question.id}
            checked={answers[question.id] === option} onChange={() => setAnswers((value) => ({ ...value, [question.id]: option }))} /> {option}</label>)}
          <label style={{ display: "grid", gap: 4 }}>{t("intent.freeText")}<textarea required value={answers[question.id] ?? ""}
            onChange={(event) => setAnswers((value) => ({ ...value, [question.id]: event.target.value }))} /></label>
        </fieldset>)}
      </form> : <p>{t("intent.timing", { elapsed: Math.ceil(task.elapsed_seconds), attempts: task.attempts,
        remaining: Math.max(0, Math.ceil(task.remaining_seconds)), budget: Math.max(0, Math.ceil(task.budget_remaining_seconds)) })}</p>}
      {task.error ? <details open><summary>{t("intent.technicalError")}</summary><p style={{ overflowWrap: "anywhere" }}>{task.error.code} — {task.error.message}</p></details> : null}
      {error ? <p role="alert">{error}</p> : null}
      <footer style={{ flexWrap: "wrap" }}>
        <Button variant="ghost" disabled={busy} onClick={() => void act("cancel")}>{t("intent.cancel")}</Button>
        {resourceError ? <Button disabled={busy} onClick={onModelSettings}>{t("intent.modelSettings")}</Button> : null}
        {clarification ? <Button form="intent-answers" type="submit" variant="primary" disabled={busy || clarification.questions.some((question) => !answers[question.id]?.trim())}>{t("intent.confirm")}</Button> : null}
        {!clarification && task.can_extend && task.status === "identifying" ? <Button disabled={busy} onClick={() => void act("extend")}>{t("intent.extend")}</Button> : null}
        {!clarification && task.can_retry && ["failed", "interrupted", "waiting_user"].includes(task.status) ? <Button disabled={busy} onClick={() => void act("retry")}>{t("intent.retry")}</Button> : null}
      </footer>
    </dialog>
  </>;
}
