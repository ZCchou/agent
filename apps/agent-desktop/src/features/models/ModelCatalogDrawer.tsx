import { Bot, Cloud, Cpu, Search, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";
import { nativeSearchRoute, nativeSearchState, type ProviderKind, type ProviderProfileSettings, type RouteHealthKey, type RouteHealthRecord } from "../../types/settings";
import { ModelCatalogItem } from "./ModelCatalogItem";
import type { ProviderModelSettings } from "../../types/settings";

interface Props {
  error?: string | null;
  provider: ProviderProfileSettings;
  routeHealth: RouteHealthRecord[];
  defaultProviderId: string;
  defaultModelId: string;
  onClose(): void;
  onEnabledChange(modelId: string, enabled: boolean): void;
  onAutomaticRoutingChange(modelId: string, allowed: boolean): void;
  onProbeTools(modelId: string): void;
  onProbeNativeSearch(modelId: string): void;
  onResetRouteHealth(key: RouteHealthKey): Promise<void>;
  onConfigureExternalSearch(): void;
  webPrivacyAcknowledged: boolean;
  onEnableWebSearch(): void;
  onSetDefault(modelId: string): void;
  onRemoveManual(modelId: string): void;
  onSaveLimits(modelId: string, contextLength: number | null, maxOutputTokens: number | null): Promise<void>;
  onSaveRouting(modelId: string, inputPrice: string | null, outputPrice: string | null,
    qualityScore: number | null, priority: number): Promise<void>;
  onResetVision(modelId: string): void;
  onAddManual(modelId: string): Promise<void>;
}

function providerIcon(kind: ProviderKind) {
  if (kind === "ollama") return <Cpu size={22} />;
  if (kind === "openai_compatible") return <Cloud size={22} />;
  return <Bot size={22} />;
}

export function filterCatalogModels(models: ProviderModelSettings[], search: string, onlyEnabled: boolean) {
  const query = search.trim().toLocaleLowerCase();
  return models.filter((model) =>
    (!onlyEnabled || model.enabled) && (!query || model.id.toLocaleLowerCase().includes(query)),
  );
}

export function ModelCatalogDrawer({
  provider, routeHealth, defaultProviderId, defaultModelId, onClose, onEnabledChange, onAutomaticRoutingChange, onProbeTools, onProbeNativeSearch, onResetRouteHealth,
  onSetDefault, onRemoveManual, onSaveLimits, onSaveRouting, onAddManual, onConfigureExternalSearch,
  webPrivacyAcknowledged, onEnableWebSearch, onResetVision,
  error,
}: Props) {
  const { t } = useTranslation("common");
  const searchInput = useRef<HTMLInputElement>(null);
  const [search, setSearch] = useState("");
  const [onlyEnabled, setOnlyEnabled] = useState(false);
  const [manualModelId, setManualModelId] = useState("");
  const [adding, setAdding] = useState(false);
  const addLock = useRef(false);
  const [addError, setAddError] = useState(false);
  const visibleModels = useMemo(
    () => filterCatalogModels(provider.models, search, onlyEnabled),
    [onlyEnabled, provider.models, search],
  );

  useEffect(() => {
    searchInput.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, provider.id]);

  async function addManual(event: FormEvent) {
    event.preventDefault();
    const value = manualModelId.trim();
    if (!value || addLock.current) return;
    addLock.current = true; setAdding(true); setAddError(false);
    try {
      await onAddManual(value);
      setManualModelId("");
    } catch {
      setAddError(true);
    } finally {
      addLock.current = false; setAdding(false);
    }
  }

  return <div className="drawer-backdrop" role="presentation" onMouseDown={onClose}>
    <aside className="project-drawer model-catalog-drawer" role="dialog" aria-modal="true"
      aria-labelledby="provider-model-list-title" onMouseDown={(event) => event.stopPropagation()}>
      <div className="drawer-header model-catalog-drawer__header">
        <div>
          <span className={`model-provider-card__icon model-provider-card__icon--${provider.kind}`} aria-hidden="true">{providerIcon(provider.kind)}</span>
          <div><h2 id="provider-model-list-title">{provider.name}</h2><p>{t("models.modelListDescription")}</p></div>
        </div>
        <button type="button" onClick={onClose} aria-label={t("actions.close")}><X size={19} /></button>
      </div>

      {error ? <p role="alert">{error}</p> : null}

      {!webPrivacyAcknowledged && nativeSearchRoute(provider) ? <div className="model-catalog-web-notice">
        <div><strong>{t("models.enableNativeSearchTitle")}</strong><p>{t("models.enableNativeSearchDescription")}</p></div>
        <Button onClick={onEnableWebSearch}>{t("models.enableNativeSearch")}</Button>
      </div> : null}

      <div className="model-list-toolbar">
        <label><Search size={16} /><input ref={searchInput} value={search} placeholder={t("models.searchModels")}
          onChange={(event) => setSearch(event.target.value)} /></label>
        <label className="model-list-toolbar__filter"><input type="checkbox" checked={onlyEnabled}
          onChange={(event) => setOnlyEnabled(event.target.checked)} />{t("models.onlyEnabled")}</label>
      </div>

      <div className="model-list" data-testid="model-catalog-scroll">
        {visibleModels.map((model) => <ModelCatalogItem key={model.id} model={model}
          routeHealth={routeHealth.filter((record) => record.model_id === model.id)}
          isCurrent={defaultProviderId === provider.id && defaultModelId === model.id}
          onEnabledChange={(enabled) => onEnabledChange(model.id, enabled)}
          onAutomaticRoutingChange={(allowed) => onAutomaticRoutingChange(model.id, allowed)}
          onProbeTools={() => onProbeTools(model.id)} onSetDefault={() => onSetDefault(model.id)}
          canProbeNativeSearch={Boolean(nativeSearchRoute(provider))}
          nativeSearchStatus={nativeSearchState(provider, model)?.support_status}
          nativeSearchProvenance={nativeSearchRoute(provider)?.source_provenance}
          onProbeNativeSearch={() => onProbeNativeSearch(model.id)}
          onResetRouteHealth={onResetRouteHealth}
          onConfigureExternalSearch={onConfigureExternalSearch}
          onRemoveManual={() => onRemoveManual(model.id)}
          onResetVision={() => onResetVision(model.id)}
          onSaveRouting={(inputPrice, outputPrice, qualityScore, priority) =>
            onSaveRouting(model.id, inputPrice, outputPrice, qualityScore, priority)}
          onSaveLimits={(contextLength, maxOutputTokens) => onSaveLimits(model.id, contextLength, maxOutputTokens)} />)}
        {!visibleModels.length ? <div className="model-list__empty"><Search size={21} /><p>{t("models.noModels")}</p></div> : null}
      </div>

      {provider.kind !== "fake" ? <form className="model-manual-form" onSubmit={(event) => void addManual(event)}>
        <label>{t("models.manualModel")}<input disabled={adding} value={manualModelId} placeholder={t("models.manualModelPlaceholder")}
          onChange={(event) => setManualModelId(event.target.value)} /></label>
        <Button type="submit" disabled={adding || !manualModelId.trim()}>{adding ? t("actions.saving") : t("models.addManual")}</Button>
        {addError ? <p role="alert">{t("buttonAudit.saveFailed")}</p> : null}
      </form> : null}
    </aside>
  </div>;
}
