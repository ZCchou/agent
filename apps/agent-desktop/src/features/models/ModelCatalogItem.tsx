import { Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";
import { routeHealthKey, type ProviderModelSettings, type RouteCapabilityStatus, type RouteHealthKey, type RouteHealthRecord } from "../../types/settings";
import { ModelContextLimits } from "./ModelContextLimits";

interface Props {
  model: ProviderModelSettings;
  routeHealth: RouteHealthRecord[];
  nativeSearchStatus?: RouteCapabilityStatus;
  nativeSearchProvenance?: "structured" | "opaque" | "none" | "unknown";
  isCurrent: boolean;
  onEnabledChange(enabled: boolean): void;
  onAutomaticRoutingChange(allowed: boolean): void;
  onProbeTools(): void;
  onProbeNativeSearch(): void;
  onResetRouteHealth(key: RouteHealthKey): Promise<void>;
  canProbeNativeSearch: boolean;
  onConfigureExternalSearch(): void;
  onSetDefault(): void;
  onRemoveManual(): void;
  onSaveLimits(contextLength: number | null, maxOutputTokens: number | null): Promise<void>;
  onSaveRouting(inputPrice: string | null, outputPrice: string | null,
    qualityScore: number | null, priority: number): Promise<void>;
  onResetVision(): void;
}

export function ModelCatalogItem({
  model, routeHealth, nativeSearchStatus, nativeSearchProvenance, isCurrent, onEnabledChange, onAutomaticRoutingChange, onProbeTools, onProbeNativeSearch, onResetRouteHealth, canProbeNativeSearch,
  onSetDefault, onRemoveManual, onSaveLimits, onSaveRouting, onConfigureExternalSearch, onResetVision,
}: Props) {
  const { t } = useTranslation("common");
  return <article className="model-catalog-item">
    <div className="model-catalog-item__header">
      <div className="model-catalog-item__identity">
        <strong title={model.id}>{model.id}</strong>
        <div className="model-catalog-item__badges">
          {isCurrent ? <span className="model-badge model-badge--primary">{t("models.currentDefault")}</span> : null}
          <span className={`model-badge model-badge--${model.status}`}>{t(`models.catalogStatus.${model.status}`)}</span>
          <span className="model-badge">{t(`models.catalogSource.${model.source}`)}</span>
          <span className="model-badge">{t(`models.toolMode.${model.tool_mode}`)}</span>
          <span className="model-badge">{t(`models.vision.${model.capabilities.vision_status ?? "unknown"}`)}</span>
          <span className="model-badge">{t(`models.nativeSearch.${nativeSearchStatus ?? model.native_web_search_status ?? "unknown"}`)}</span>
          {(model.capability_states ?? []).map((state) => <span
            key={`capability:${state.capability_id}`}
            className={`model-badge model-badge--${state.verified_status === "unsupported" ? "unavailable" : state.verified_status}`}
            title={state.error_code ?? state.capability_id}>
            {state.capability_id}: {state.verified_status}
          </span>)}
          {routeHealth.map((record) => record.health_status === "ready" ? null : <span
            key={`${record.route_id}:${record.capability_signature.join(",")}`}
            className={`model-badge model-badge--${record.health_status === "configuration_blocked" ? "unavailable" : "unverified"}`}
            title={`${record.route_id} · ${record.capability_signature.join("+")}${record.last_error_code ? ` · ${record.last_error_code}` : ""}`}>
            {t(`models.routeHealth.${record.health_status}`, {
              seconds: record.retry_at ? Math.max(0, Math.ceil((Date.parse(record.retry_at) - Date.now()) / 1000)) : 0,
            })}
          </span>)}
          {canProbeNativeSearch ? <span className="model-badge" title={t("models.sourceProvenanceHint")}>
            {t(`models.sourceProvenance.${nativeSearchProvenance ?? "unknown"}`)}
          </span> : null}
        </div>
      </div>
      <div className="model-catalog-item__switches">
        <label className="model-enable-switch">
          <input type="checkbox" checked={model.enabled}
            disabled={isCurrent || (model.status === "unavailable" && !model.enabled)}
            onChange={(event) => onEnabledChange(event.target.checked)} />
          <span aria-hidden="true" className="model-enable-switch__track"><span /></span>
          <span>{t("models.allowUse")}</span>
        </label>
        <label className="model-enable-switch" title={t("models.allowAutomaticHint")}>
          <input type="checkbox" checked={model.allow_automatic_routing}
            disabled={isCurrent || !model.enabled || model.status === "unavailable"}
            onChange={(event) => onAutomaticRoutingChange(event.target.checked)} />
          <span aria-hidden="true" className="model-enable-switch__track"><span /></span>
          <span>{t("models.allowAutomatic")}</span>
        </label>
      </div>
    </div>

    <ModelContextLimits model={model} onSave={onSaveLimits} onSaveRouting={onSaveRouting} />

    <div className="model-catalog-item__actions">
      <div>
        {model.enabled && model.tool_mode === "unknown"
          ? <Button variant="ghost" onClick={onProbeTools}>{t("models.probeTools")}</Button> : null}
        {canProbeNativeSearch
          ? <Button variant="ghost" disabled={!model.enabled} title={t("models.probeNativeSearchCostHint")} onClick={onProbeNativeSearch}>{t("models.probeNativeSearch")}</Button>
          : <Button variant="ghost" onClick={onConfigureExternalSearch}>{t("models.configureExternalSearch")}</Button>}
        {model.capabilities.vision_source === "user_confirmed" || model.capabilities.vision_source === "runtime"
          ? <Button variant="ghost" onClick={onResetVision}>{t("models.resetVision")}</Button> : null}
        {routeHealth.some((record) => record.health_status !== "ready")
          ? <Button variant="ghost" onClick={() => void (async () => {
            for (const record of routeHealth.filter((item) => item.health_status !== "ready")) {
              await onResetRouteHealth(routeHealthKey(record));
            }
          })()}>{t("models.resetRouteHealth")}</Button> : null}
        {model.enabled && model.status !== "unavailable" && model.capabilities.text !== "no" && !isCurrent
          ? <Button onClick={onSetDefault}>{t("models.setAsDefault")}</Button> : null}
      </div>
      {model.source === "manual" && !isCurrent
        ? <Button variant="ghost" className="model-catalog-item__danger" icon={<Trash2 size={14} />}
          onClick={onRemoveManual}>{t("models.removeManual")}</Button> : null}
    </div>
  </article>;
}
