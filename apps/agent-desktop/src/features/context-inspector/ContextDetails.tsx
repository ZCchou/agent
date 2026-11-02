import { ChevronRight, RotateCcw, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { contextRepository } from "../../services/runtime";
import type { ContextItem, ContextSnapshot, ContextSourcePreview } from "../../types/domain";

const PREVIEWABLE = new Set(["message", "current_message", "project_knowledge", "global_instructions", "attachment"]);
const SAFE_IMAGE = /^data:image\/(?:png|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/;

function metadataString(item: ContextItem, key: string): string | null {
  const value = item.metadata?.[key];
  return typeof value === "string" && value.trim() ? value : null;
}

function attachmentRange(item: ContextItem): { start: number; end: number } | null {
  const { source_start: start, source_end: end } = item;
  return Number.isInteger(start) && Number.isInteger(end) && (start as number) >= 0 && (end as number) > (start as number)
    ? { start: start as number, end: end as number } : null;
}

function attachmentReason(item: ContextItem, hasRange: boolean): string {
  if (item.included) return hasRange ? "context.attachmentSelected" : "context.attachmentIncluded";
  const reasons: Record<string, string> = {
    token_budget: "context.attachmentBudgetOmitted",
    model_vision_unsupported: "context.attachmentVisionUnsupported",
    model_vision_confirmation_required: "context.attachmentVisionUnconfirmed",
    specialist_image_not_resent: "context.attachmentSpecialistHandled",
  };
  return reasons[item.exclusion_reason ?? ""] ?? "context.attachmentOmitted";
}

function imageDataUrl(source: ContextSourcePreview): string | null {
  const value = source.source?.data_url;
  return typeof value === "string" && SAFE_IMAGE.test(value) ? value : null;
}

interface Props {
  runId: string;
  snapshot: ContextSnapshot | null;
  loading: boolean;
  error: string | null;
  retrying?: boolean;
  onExpandedRetry?(): Promise<void> | void;
}

export function ContextDetails({ runId, snapshot, loading, error, retrying, onExpandedRetry }: Props) {
  const { t } = useTranslation("chat");
  const [source, setSource] = useState<ContextSourcePreview | null>(null);
  const [sourceIdentity, setSourceIdentity] = useState<string | null>(null);
  const [sourceItemId, setSourceItemId] = useState<string | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [sourceLoading, setSourceLoading] = useState(false);
  const sourceRequest = useRef(0);
  const identity = `${runId}:${snapshot?.id ?? ""}`;
  const currentIdentity = useRef(identity);
  currentIdentity.current = identity;

  useEffect(() => {
    sourceRequest.current += 1;
    setSource(null);
    setSourceIdentity(null);
    setSourceItemId(null);
    setSourceError(null);
    setSourceLoading(false);
  }, [identity]);

  useEffect(() => () => { sourceRequest.current += 1; }, []);

  if (loading) return <p className="state-message">{t("context.loading")}</p>;
  if (error) return <p className="state-message state-message--error">{error}</p>;
  if (!snapshot) return <p className="state-message">{t("context.empty")}</p>;

  const canExpand = snapshot.compression_applied === 1
    || snapshot.omitted_items > 0
    || ["low", "none"].includes(snapshot.retrieval_confidence);
  const previewedItem = snapshot.items.find((item) => item.id === sourceItemId);
  const previewImageUrl = source ? imageDataUrl(source) : null;

  async function openSource(itemId: string) {
    const request = ++sourceRequest.current;
    const openedIdentity = identity;
    setSource(null);
    setSourceIdentity(null);
    setSourceItemId(itemId);
    setSourceLoading(true);
    setSourceError(null);
    try {
      const preview = await contextRepository.source(runId, itemId);
      if (request !== sourceRequest.current || openedIdentity !== currentIdentity.current) return;
      if (preview.item_id !== itemId || (typeof preview.source?.content !== "string" && !imageDataUrl(preview))) {
        setSourceError(t("context.sourceUnavailable"));
      } else {
        setSource(preview);
        setSourceIdentity(openedIdentity);
      }
    } catch (value) {
      if (request === sourceRequest.current && openedIdentity === currentIdentity.current) {
        const code = value instanceof Error ? value.message : String(value);
        setSourceError(["context_source_changed", "attachment_changed", "image_integrity_failed"].some((item) => code.includes(item))
          ? t("context.sourceChanged") : t("context.sourceUnavailable"));
      }
    } finally {
      if (request === sourceRequest.current && openedIdentity === currentIdentity.current) setSourceLoading(false);
    }
  }

  return <section className="context-panel">
    <div className="context-panel__summary">
      <strong>{t("context.used", { used: snapshot.estimated_tokens, budget: snapshot.input_target })}</strong>
      <span>{t("context.window", { window: snapshot.context_window, reserve: snapshot.output_reserve })}</span>
      <span>{t("context.policy", { policy: snapshot.policy_version, generation: snapshot.context_generation })}</span>
      {snapshot.global_instruction_revision > 0 ? <span>{t("context.globalInstructions", {
        revision: snapshot.global_instruction_revision,
      })}</span> : null}
      <span>{t("context.retrieval", { confidence: t(`context.confidence.${snapshot.retrieval_confidence}`) })}</span>
      {snapshot.compaction ? <span>{t("context.compactionUsage", {
        input: snapshot.compaction.input_tokens ?? t("context.unknownUsage"),
        output: snapshot.compaction.output_tokens ?? t("context.unknownUsage"),
      })}</span> : null}
      {snapshot.compaction?.estimated_cost !== null && snapshot.compaction?.estimated_cost !== undefined
        ? <span>{t("context.estimatedCost", { cost: snapshot.compaction.estimated_cost.toFixed(6) })}</span>
        : null}
      <span>{snapshot.run_usage?.cached_input_tokens !== null
        && snapshot.run_usage?.cached_input_tokens !== undefined
        ? t("context.cachedTokens", { count: snapshot.run_usage.cached_input_tokens })
        : t("context.cacheEstimated", { hash: snapshot.stable_prefix_hash.slice(0, 12) })}</span>
    </div>
    <ul>{snapshot.items.map((item) => {
      const range = item.source_type === "attachment" ? attachmentRange(item) : null;
      const attachmentName = item.source_type === "attachment" ? metadataString(item, "name") : null;
      const route = metadataString(item, "receiving_route");
      const routeKey = route === "legacy.primary" ? "direct"
        : route === "legacy.native_search" || route?.endsWith(".anthropic_web") ? "nativeSearch"
        : route ? "authorized" : "unknown";
      return <li key={item.id}>
      <span aria-hidden="true">{item.included ? "✓" : "–"}</span>
      <span>
        {t(`context.sources.${item.source_type}`, { defaultValue: item.source_type })}
        {attachmentName ? <strong className="context-panel__attachment-name">{attachmentName}</strong> : null}
        <small>{t(`context.trust.${item.trust}`, { defaultValue: item.trust })}</small>
        {item.source_type === "attachment" ? <>
          {range ? <small>{t("context.attachmentRange", range)}</small> : null}
          <small>{t(`context.attachmentRoute.${routeKey}`)}</small>
          <small>{t(attachmentReason(item, Boolean(range)))}</small>
        </> : null}
      </span>
      {item.source_type !== "attachment" && item.exclusion_reason ? <small>{item.exclusion_reason}</small> : null}
      {item.source_id && PREVIEWABLE.has(item.source_type)
        ? <button type="button" className="context-panel__source-button" onClick={() => void openSource(item.id)}>
          {t("context.previewSource")}<ChevronRight size={14} />
        </button> : null}
    </li>;
    })}</ul>
    {sourceLoading ? <p className="state-message">{t("context.sourceLoading")}</p> : null}
    {sourceError ? <p className="state-message state-message--error">{sourceError}</p> : null}
    {source && sourceItemId && sourceIdentity === identity ? <div className="context-source-preview">
      <button type="button" aria-label={t("context.closeSource")} onClick={() => {
        sourceRequest.current += 1; setSource(null); setSourceIdentity(null); setSourceItemId(null);
      }}><X size={14} /></button>
      <strong>{previewedItem?.source_type === "attachment"
        ? metadataString(previewedItem, "name") ?? t("context.sources.attachment")
        : t(`context.sources.${source.source_type}`, { defaultValue: source.source_type })}</strong>
      {previewImageUrl ? <img src={previewImageUrl} alt={t("context.sourceImageAlt")} />
        : <pre>{typeof source.source.content === "string" ? source.source.content : ""}</pre>}
    </div> : null}
    {canExpand && onExpandedRetry ? <button type="button" className="context-panel__retry"
      disabled={retrying} onClick={() => void onExpandedRetry()}>
      <RotateCcw size={14} />{retrying ? t("context.expandedRetrying") : t("context.expandedRetry")}
    </button> : null}
  </section>;
}
