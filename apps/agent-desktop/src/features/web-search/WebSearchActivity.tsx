import { AlertTriangle, Search } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { WebOperation } from "../../types/domain";
import { WebSources } from "./WebSources";

export function WebSearchActivity({ operation }: { operation: WebOperation }) {
  const { t } = useTranslation("chat");
  const failed = ["failed", "interrupted", "unknown_outcome"].includes(operation.status);
  const evidenceWarning = operation.status === "completed"
    && operation.evidence_status !== "traceable_sources";
  const routeDetail = operation.route === "native" ? operation.model_id : operation.backend_id;
  return <article className={`web-search-activity${failed ? " web-search-activity--failed" : ""}${evidenceWarning ? " web-search-activity--warning" : ""}`}>
    <div className="web-search-activity__header">
      {failed ? <AlertTriangle size={15} /> : <Search size={15} />}
      <strong>{t(`webSearch.operationStatus.${operation.status}`)}</strong>
      <span>{t(`webSearch.route.${operation.route}`)}{routeDetail ? ` · ${routeDetail}` : ""}</span>
    </div>
    {operation.status === "completed" ? <p className="web-search-activity__evidence">
      {operation.evidence_status === "traceable_sources"
        ? t("webSearch.evidence.grounded", { count: operation.verified_source_count })
        : t(`webSearch.evidence.${operation.evidence_status}`)}
    </p> : null}
    <p className="web-search-activity__query">{t("webSearch.query", { query: operation.query_text })}</p>
    {typeof operation.usage.credits === "number" ? <p className="web-search-activity__usage">
      {t("webSearch.credits", { count: operation.usage.credits })}
    </p> : operation.usage.managed_cost === "none" ? <p className="web-search-activity__usage">
      {t("webSearch.noManagedCost")}
    </p> : typeof operation.usage.search_calls === "number" ? <p className="web-search-activity__usage">
      {t("webSearch.searchCalls", { count: operation.usage.search_calls })}
    </p> : null}
    <WebSources sources={operation.sources} />
    {operation.error_code ? <p className="web-search-activity__error">
      {t(`errors.${operation.error_code}`, { defaultValue: operation.error_code })}
    </p> : null}
  </article>;
}
