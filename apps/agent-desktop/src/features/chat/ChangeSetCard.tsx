import { FileDiff, ShieldAlert } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";
import { changeSetRepository } from "../../services/runtime";
import type { ChangeSet } from "../../types/domain";
import { UndoChangeDialog } from "./UndoChangeDialog";
import { changeSetSelection } from "./changeSetSelection";

export function ChangeSetCard({ changeSet, projectName, conversationTitle, expanded = false, onResolved }: {
  changeSet: ChangeSet;
  projectName?: string;
  conversationTitle?: string;
  expanded?: boolean;
  onResolved(): void;
}) {
  const { t } = useTranslation("chat");
  const waiting = changeSet.status === "waiting_user" || changeSet.status === "reviewing";
  const recoveryPending = changeSet.status === "recovery_required" || changeSet.status === "rollback_conflicted";
  const [busy, setBusy] = useState(false);
  const plan = changeSet.plan;
  const planKey = plan ? `${plan.plan_id}:${plan.plan_hash}` : "";
  const [selected, setSelected] = useState<{ key: string; ids: string[] } | null>(null);
  const selection = changeSetSelection(plan, selected?.key === planKey ? selected.ids : undefined);
  const steps = waiting && selection && plan ? plan.steps : changeSet.mutations.map((mutation, index) => ({
    mutation, operation_id: `legacy-${index}`, depends_on: [], preconditions: {},
  }));
  const risk = plan?.effective_risk ?? changeSet.risk;
  const [filesOpen, setFilesOpen] = useState(waiting || recoveryPending || expanded);
  const [undoOpen, setUndoOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function resolve(approve: boolean) {
    if (!plan || !selection || busy) return;
    setBusy(true);
    setActionError(null);
    try {
      await changeSetRepository.resolve(changeSet.id, { approve, planId: plan.plan_id, planHash: plan.plan_hash,
        approvedOperationIds: approve ? selection.approved : [] });
      onResolved();
    } catch (value) {
      setActionError(value instanceof Error ? value.message : String(value));
    } finally { setBusy(false); }
  }

  async function undo() {
    if (changeSet.undo_state !== "available") return;
    setBusy(true);
    setActionError(null);
    try {
      await changeSetRepository.undo(changeSet.id);
      setUndoOpen(false);
      onResolved();
    } catch (value) {
      const message = value instanceof Error ? value.message : String(value);
      const code = message.split(":", 1)[0];
      setActionError(t(`changes.undoErrors.${code}`, { defaultValue: message }));
      onResolved();
    } finally { setBusy(false); }
  }

  async function recover() {
    if (!recoveryPending || busy) return;
    setBusy(true);
    setActionError(null);
    try {
      await changeSetRepository.recover(changeSet.id);
      onResolved();
    } catch (value) {
      const message = value instanceof Error ? value.message : String(value);
      const code = message.split(":", 1)[0];
      setActionError(t(`changes.recoveryErrors.${code}`, { defaultValue: t("changes.recoveryErrors.generic") }));
    } finally { setBusy(false); }
  }

  return (
    <section className={`change-set-card change-set-card--${risk}`}>
      <header><ShieldAlert size={17} /><strong>{changeSet.title}</strong><span>{t(`changes.risk.${risk}`)}</span></header>
      {waiting && !selection ? <p role="status">{t("changes.replanRequired")}</p> : null}
      {plan && selection ? <p className="change-set-card__plan"><span>{t("changes.planFingerprint")}</span> <code>{plan.plan_hash}</code></p> : null}
      <details open={filesOpen} onToggle={(event) => setFilesOpen(event.currentTarget.open)}>
        <summary><FileDiff size={15} />{t("changes.files", { count: steps.length })}</summary>
        <ul>{steps.map(({ mutation, operation_id }) => (
          <li key={operation_id}>
            {changeSet.status === "waiting_user" && selection ? <input type="checkbox" checked={selection.approved.includes(operation_id)}
              aria-label={t("changes.selectOperation", { path: mutation.path })}
              disabled={busy || selection.required.includes(operation_id)}
              onChange={(event) => setSelected({ key: planKey, ids: event.target.checked
                ? [...selection.explicit, operation_id] : selection.explicit.filter((item) => item !== operation_id) })} /> : null}
            <code>{mutation.kind}</code><span>{mutation.source_path ? `${mutation.source_path} → ` : ""}{mutation.path}</span>
            {selection?.required.includes(operation_id) ? <small>{t("changes.requiredDirectory")}</small> : null}
            {mutation.kind === "patch_text" ? <pre>
              {`− ${mutation.old_text ?? ""}\n+ ${mutation.new_text ?? ""}`.slice(0, 1200)}
              {(mutation.old_text?.length ?? 0) + (mutation.new_text?.length ?? 0) > 1200 ? "…" : ""}
            </pre> : mutation.content ? <pre>{mutation.content.slice(0, 1200)}{mutation.content.length > 1200 ? "…" : ""}</pre> : null}
          </li>
        ))}</ul>
      </details>
      {changeSet.status === "reviewing" && selection ? <p>{t("changes.aiReviewing")}</p> : null}
      {changeSet.status === "waiting_user" && selection ? <div className="change-set-card__actions">
        <Button onClick={() => void resolve(false)} disabled={busy}>{t("changes.reject")}</Button>
        <Button variant="primary" onClick={() => void resolve(true)} disabled={busy || selection.approved.length === 0}>
          {selection.approved.length === steps.length ? t("changes.approve") : t("changes.approveSelected", { count: selection.approved.length })}
        </Button>
      </div> : null}
      {!waiting ? <p>{t(`changes.status.${changeSet.status}`)}</p> : null}
      {recoveryPending ? <div className="change-set-card__recovery">
        <p>{t("changes.recoveryNextStep")}</p>
        <p>{t("changes.recoveryPreservesExternal")}</p>
        {changeSet.recovery_error_code ? <p className="change-set-card__error">
          {t(`changes.recoveryErrors.${changeSet.recovery_error_code}`, { defaultValue: t("changes.recoveryErrors.generic") })}
        </p> : null}
        {(changeSet.recovery_affected_paths?.length ?? 0) > 0 ? <>
          <strong>{t("changes.affectedPaths")}</strong>
          <ul className="change-set-card__affected-paths">{changeSet.recovery_affected_paths?.map((path, index) =>
            <li key={`${index}:${path}`}><code>{path}</code></li>)}</ul>
        </> : null}
        <div className="change-set-card__actions">
          <Button onClick={() => void recover()} disabled={busy}>{t("changes.recheckRecovery")}</Button>
        </div>
      </div> : null}
      {changeSet.status === "completed" && changeSet.recovery_point_id ? <>
        {changeSet.undo_state === "available" ? <div className="change-set-card__actions">
          <Button onClick={() => setUndoOpen(true)} disabled={busy}>{t("changes.undo")}</Button>
        </div> : <p className="change-set-card__undo-state">{t(changeSet.undo_block_reason === "recovery_unverified"
          ? "changes.recoveryErrors.recovery_unverified" : `changes.undoState.${changeSet.undo_state}`, {
          defaultValue: t("changes.undoState.unavailable"),
        })}</p>}
      </> : null}
      {actionError ? <p className="change-set-card__error">{actionError}</p> : null}
      <UndoChangeDialog open={undoOpen} changeSet={changeSet} projectName={projectName}
        conversationTitle={conversationTitle} busy={busy} onCancel={() => setUndoOpen(false)} onConfirm={() => void undo()} />
    </section>
  );
}
