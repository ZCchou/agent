import { useTranslation } from "react-i18next";
import type { MessageAttachment, ToolOperation } from "../../types/domain";

const labels = new Map([
  ["document.inspect_template", "inspect"], ["document.read_attachment", "read"],
  ["document.review_targets", "review"], ["rag.search", "search"], ["rag.read", "source"],
  ["document.export", "export"],
]);

export function ReviewOperationStream({ operations, attachments, runId }: {
  operations: ToolOperation[]; attachments: MessageAttachment[]; runId: string;
}) {
  const { t } = useTranslation("chat");
  const recent = [...new Map(operations.filter(op => op.run_id === runId)
    .map(op => [op.id, op])).values()].sort((a, b) => a.sequence - b.sequence).slice(-5);
  if (!recent.length) return null;
  return <section className="execution-task-card" aria-label={t("publicOps.title")}>
    <strong>{t("publicOps.title")}</strong>
    <ol aria-live="polite" aria-relevant="additions text">
      {recent.map(op => {
        const args = op.arguments ?? {};
        const name = attachments.find(a => a.id === args.attachment_id)?.name;
        const target = name ?? (typeof args.path === "string" ? args.path : undefined);
        const query = op.tool_name === "rag.search" && typeof args.query === "string" ? args.query : null;
        return <li key={op.id} value={op.sequence}>
          {labels.has(op.tool_name) ? t("publicOps." + labels.get(op.tool_name)) : op.tool_name}{target && <> · {target}</>}
          {query && <> · {query.slice(0, 160)}</>} · {t("tools.operationStatus." + op.status)}
          {op.status === "failed" && op.error_code && <> · {t("tools.errorReasons." + op.error_code, { defaultValue: op.error_code })}</>}
        </li>;
      })}
    </ol>
  </section>;
}
