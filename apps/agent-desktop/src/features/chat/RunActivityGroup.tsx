import { ChevronDown, FolderSearch } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ConversationTimelineItem } from "./timelineModel";
import { ChangeSetCard } from "./ChangeSetCard";
import { WebSearchActivity } from "../web-search/WebSearchActivity";
import { SearchAuditLabel } from "./SearchAuditLabel";
import { searchPresentation } from "./searchPresentation";

import { TerminalCommandCard, terminalResult } from "./TerminalCommandCard";
import { AssistantMessageBody } from "./markdown/AssistantMessageBody";
import type { ProjectDirectoryTarget, ProjectFileTarget } from "./markdown/markdownPolicy";
import { pauseProcessFollowing } from "./useConversationScroll";

const TERMINAL_TOOL = new Set(["completed", "rejected", "failed", "cancelled", "interrupted"]);
const TERMINAL_CHANGE = new Set(["completed", "undone", "rejected", "expired", "cancelled_scope_changed", "recovery_required", "rollback_conflicted", "rolled_back"]);
const RECOVERY_ATTENTION = new Set(["recovery_required", "rollback_conflicted"]);

export function RunActivityGroup({ item, projectName, conversationTitle, onResolved, onProjectFile, onProjectDirectory }: {
  item: Extract<ConversationTimelineItem, { kind: "activity" }>;
  projectName?: string;
  conversationTitle?: string;
  onProjectFile?: (target: ProjectFileTarget) => void;
  onProjectDirectory?: (target: ProjectDirectoryTarget) => void;
  onResolved(): void;
}) {
  const { t } = useTranslation("chat");
  const active = item.operations.some((operation) => !TERMINAL_TOOL.has(operation.status))
    || item.changeSets.some((changeSet) => !TERMINAL_CHANGE.has(changeSet.status))
    || item.webOperations.some((operation) => ![
      "completed", "failed", "cancelled", "interrupted", "unknown_outcome",
    ].includes(operation.status));
  const terminalFailure = item.operations.some((operation) => {
    const result = terminalResult(operation);
    return result && (result.exit_code !== 0 || result.timed_out || result.cancelled);
  });
  const failed = terminalFailure || item.operations.some((operation) => ["failed", "interrupted"].includes(operation.status))
    || item.webOperations.some((operation) => ["failed", "interrupted", "unknown_outcome"].includes(operation.status))
    || item.events.some(({ summary }) => ["failed", "interrupted"].includes(summary.status))
    || item.changeSets.some((changeSet) => RECOVERY_ATTENTION.has(changeSet.status));
  const recoveryAttention = item.changeSets.some((changeSet) => RECOVERY_ATTENTION.has(changeSet.status));
  const rolledBack = item.changeSets.some((changeSet) => changeSet.status === "rolled_back");
  const evidenceWarning = item.webOperations.some((operation) =>
    operation.status === "completed" && operation.evidence_status !== "traceable_sources");
  const displayOperations = item.operations.length ? item.operations.map((operation) => ({
    ...operation,
    search_completeness: operation.search_completeness ?? item.events.find(({ summary }) =>
      summary.operation_id === operation.id)?.summary.search_completeness ?? null,
  })) : item.events.map(({ message, summary }, index) => ({
    id: message.id, tool_name: summary.tool_name, status: summary.status, sequence: index,
    search_completeness: summary.search_completeness,
  }));
  const searchStates = displayOperations.filter((operation) => operation.tool_name === "fs.search"
    && operation.status === "completed").map((operation) => searchPresentation(operation.search_completeness).state);
  const searchIncomplete = searchStates.includes("incomplete");
  const searchUnknown = searchStates.includes("unknown");
  const needsAction = recoveryAttention || item.operations.some((operation) => operation.status === "waiting_user")
    || item.changeSets.some((changeSet) => changeSet.status === "waiting_user" || changeSet.status === "ready");
  const [expanded, setExpanded] = useState<boolean | null>(null);
  const open = expanded ?? needsAction;
  useEffect(() => { if (needsAction) setExpanded(true); }, [needsAction]);
  const mutationCount = item.changeSets.reduce((total, changeSet) => total + changeSet.mutations.length, 0);
  const count = mutationCount || Math.max(item.events.length, item.operations.length) + item.webOperations.length;
  const onlyWeb = item.webOperations.length > 0 && !mutationCount && !item.operations.length && !item.events.length;
  const onlyRouting = Boolean(item.executionSummary) && !item.webOperations.length
    && !mutationCount && !item.operations.length && !item.events.length;
  const visibleSteps = item.executionSummary?.steps.filter((step) => step.status !== "skipped") ?? [];
  const routedModels = new Set(visibleSteps.map((step) =>
    `${step.provider_profile_id}:${step.model_id}`) ?? []);
  const routeLabel = (step: NonNullable<typeof item.executionSummary>["steps"][number]) => {
    const adapter = step.adapter_id ?? step.invocation_route_id;
    const protocol = step.protocol_id?.replace(/_/g, " ");
    return [adapter, protocol].filter(Boolean).join(" · ") || "—";
  };

  return <section className={`run-activity ${active ? "run-activity--active" : ""} ${failed ? "run-activity--failed" : ""}`}>
    <button type="button" className="run-activity__summary" aria-expanded={open} onClick={(event) => {
      if (!open) pauseProcessFollowing(event.currentTarget);
      setExpanded(!open);
    }}>
      <FolderSearch size={15} />
      <span>{recoveryAttention ? t("changes.activityRecoveryAttention")
        : searchIncomplete ? t("tools.searchIncomplete")
        : searchUnknown ? t("tools.searchUnknown")
        : evidenceWarning ? t("webSearch.activityMissingEvidence")
        : item.progress.length && !needsAction ? t("process.title")
        : rolledBack ? t("changes.activityRolledBack")
        : item.operations.some((operation) => operation.tool_name === "terminal.run") ? t(active ? "terminal.activityActive" : "terminal.activityComplete")
        : onlyRouting
        ? t(item.executionSummary?.selection_mode === "fixed"
          ? "routing.activityPath" : "routing.activityComplete", { count: routedModels.size })
        : onlyWeb
        ? t(active ? "webSearch.activityActive" : evidenceWarning
          ? "webSearch.activityMissingEvidence" : "webSearch.activityComplete", { count })
        : t(active ? "tools.progressActive" : "tools.progressComplete", { count })}</span>
      <ChevronDown size={14} className={open ? "run-activity__chevron--open" : ""} />
    </button>
    {open ? <div className="run-activity__details">
      {item.progress.length ? <div className="work-process__body work-process__body--saved">
        <AssistantMessageBody content={item.progress.map(({ summary }) => summary.content).join("\n\n")} state="complete"
          onProjectFile={onProjectFile} onProjectDirectory={onProjectDirectory} />
      </div> : null}
      {item.executionSummary ? <div className="routing-activity-details">
        <strong>{t("routing.coordinator", { model: item.executionSummary.coordinator_model_id })}</strong>
        {item.executionSummary.intent_resolution?.source ? <small className="routing-activity-details__intent">
          {t("routing.intentResolved", {
            source: item.executionSummary.intent_resolution.source,
            confidence: item.executionSummary.intent_resolution.confidence ?? "",
          })}
        </small> : null}
        {item.executionSummary.intent_resolution?.pending_action_id ? <small className="routing-activity-details__intent">
          {t("routing.followUpPending")}
        </small> : null}
        <ol>{visibleSteps.map((step, index) => <li key={`${step.step_type}-${index}`}>
          <span>{t(`routing.steps.${step.step_type}`)}</span>
          <code>{step.model_id}</code>
          <small>{routeLabel(step)}</small>
        </li>)}</ol>
        {item.executionSummary.invocations?.length ? <>
          <strong>{t("routing.actualInvocations")}</strong>
          <ol>{item.executionSummary.invocations.map((invocation) => <li key={invocation.id}>
            <span>{t("routing.invocationRound", { round: invocation.round_no })}</span>
            <code>{invocation.model_id ?? invocation.provider_or_backend_id}</code>
            <small>{invocation.adapter_id} · {invocation.protocol_id.replace(/_/g, " ")}</small>
          </li>)}</ol>
        </> : null}
      </div> : null}
      {item.changeSets.map((changeSet) => <ChangeSetCard key={changeSet.id} changeSet={changeSet}
        projectName={projectName} conversationTitle={conversationTitle} expanded onResolved={onResolved} />)}
      {item.operations.filter((operation) => operation.tool_name === "terminal.run").map((operation) =>
        <TerminalCommandCard key={operation.id} operation={operation} onResolved={onResolved} />)}
      {item.webOperations.map((operation) => <WebSearchActivity key={operation.id} operation={operation} />)}
      {item.operations.length || item.events.length ? <ol className="run-activity__operations">
        {displayOperations.map((operation) => <li key={operation.id}>
          <code>{operation.tool_name}</code><span>{t(`tools.operationStatus.${operation.status}`)}</span>
          {operation.tool_name === "fs.search" ? <SearchAuditLabel value={operation.search_completeness} /> : null}
        </li>)}
      </ol> : null}
    </div> : null}
  </section>;
}
