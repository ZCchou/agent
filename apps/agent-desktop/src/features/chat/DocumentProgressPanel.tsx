import { useTranslation } from "react-i18next";
import type { DocumentProgress } from "../../services/tauri/executionTasks";

const roles = ["include", "exclude", "context", "uncertain"] as const;
const count = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

export function DocumentProgressPanel({ progress }: { progress?: DocumentProgress | null }) {
  const { t } = useTranslation("chat");
  if (!progress) return null;
  const { phase, completed, total, batch_count, counts, latest_review, search_count } = progress;
  const scope = phase === "scope_selection" || phase === "source_selection" || phase === "source_role_review";
  const recent = Array.isArray(progress.recent) ? progress.recent.slice(-3) : [];
  const knownCounts = roles.filter(role => count(counts?.[role]));
  const label = count(completed) && count(total) && total > 0 && completed <= total &&
    (scope || phase === "document_review")
    ? t(phase === "source_role_review" ? "docProgress.rolesReviewed" : scope ? "attachments.processing.scopeSelection" : "attachments.processing.documentReview", { completed, total })
    : null;
  return <section aria-label={t("docProgress.title")} className="execution-task-card__document">
    {progress.event && ["reading","inventory_ready","selecting","checking","searching","search_results","results_saved","correcting"].includes(progress.event)
      && <strong>{t("publicOps.events." + progress.event)}</strong>}
    {label && <strong>{label}</strong>}
    {progress.event === "correcting" && progress.correction && <p>
      {t("publicOps.correctionAttempt", {value:progress.correction.attempt})} · {progress.correction.reason}
    </p>}
    {phase === "runtime_integrity" && count(completed) && count(total) && <p>{t("publicOps.integrity", { completed, total })}</p>}
    {!!progress.current?.length && <ul>{progress.current.map(item => <li key={item.target_id}>
      <code>{item.target_id}</code>{item.excerpt && <> · <q>{item.excerpt}</q></>}
    </li>)}</ul>}
    {progress.query && <p>{t("publicOps.search")} · {progress.query}</p>}
    {count(progress.hit_count) && <p>{t("publicOps.hits", { value: progress.hit_count })}</p>}
    <div className="execution-task-card__progress">
      {count(batch_count) && <span>{t("docProgress.batches", { value: batch_count })}</span>}
      {knownCounts.map(role => <span key={role}>{t("docProgress." + role)} {counts?.[role]}</span>)}
      {count(search_count) && <span>{t("docProgress.searches", { value: search_count })}</span>}
    </div>
    {recent.length > 0 && <><strong>{t("docProgress.decisions")}</strong>
      <ul>{recent.map((row, index) => <li key={row.target_id + index}>
        <code>{row.target_id}</code> · {roles.some(role => role === row.decision)
          ? t("docProgress." + row.decision) : t("docProgress.uncertain")} · {row.reason}
        {row.excerpt && <q>{row.excerpt}</q>}
      </li>)}</ul></>}
    {latest_review?.summary && <><strong>{t("docProgress.finding")}</strong>
      <p><code>{latest_review.target_id}</code> · {latest_review.summary}</p>
      {latest_review.citations?.length ? <><span>{t("docProgress.citations")}</span>
        <ul>{latest_review.citations.map((citation, index) => <li key={index}>{citation}</li>)}</ul></> : null}
    </>}
  </section>;
}
