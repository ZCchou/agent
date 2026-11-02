import { useTranslation } from "react-i18next";
import { searchPresentation } from "./searchPresentation";

export function SearchAuditLabel({ value }: { value: unknown }) {
  const { t } = useTranslation("chat");
  const scan = searchPresentation(value);
  return <small className={`search-audit search-audit--${scan.state}`}>
    {scan.state === "incomplete" ? t("tools.searchIncompleteDetail", {
      count: scan.inspectedFiles,
      reason: t(`tools.searchStopReason.${scan.reason}`),
    }) : scan.state === "complete" ? t("tools.searchCompleteDetail", { count: scan.inspectedFiles })
      : t("tools.searchUnknown")}
  </small>;
}
