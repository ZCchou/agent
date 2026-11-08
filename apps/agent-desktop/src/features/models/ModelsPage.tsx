import { LocalModelsPanel } from "./LocalModelsPanel";
import { isLocalProvider } from "../../services/modelSelection";
import {
  Bot, CheckCircle2, Cloud, Cpu, KeyRound, List, LoaderCircle, Plus, Power,
  RefreshCw, Server, Trash2, X, XCircle,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { modelCatalogRepository, providerRepository, settingsRepository, webSearchRepository } from "../../services/runtime";
import {
  nativeSearchState, type AppSettings, type ProviderKind, type ProviderPresetId,
  type RouteHealthRecord, type RoutingPreferences,
} from "../../types/settings";
import { ModelCatalogDrawer } from "./ModelCatalogDrawer";
import { RoutingSettingsCard } from "./RoutingSettingsCard";
import { useApp } from "../../contexts/AppContext";

type ProviderStatus = "idle" | "loading" | "ok" | "error";
type Preset = { id: ProviderPresetId; kind: ProviderKind; name: string; baseUrl: string };
type Confirmation =
  | { kind: "remove-manual"; providerId: string; modelId: string; count: number }
  | { kind: "disable-model"; providerId: string; modelId: string; count: number }
  | { kind: "disable-automatic"; providerId: string; modelId: string; count: number }
  | { kind: "disable-provider"; providerId: string; providerName: string; count: number }
  | { kind: "delete-provider"; providerId: string; providerName: string; count: number };

function providerIcon(kind: ProviderKind) {
  if (kind === "ollama") return <Cpu size={22} />;
  if (kind === "openai_compatible") return <Cloud size={22} />;
  return <Bot size={22} />;
}

export function ModelsPage() {
  const { t } = useTranslation("common");
  const { setCurrentPage, coreOnline } = useApp();
  const presets: Preset[] = [
    { id: "ollama", kind: "ollama", name: "Ollama", baseUrl: "http://127.0.0.1:11434" },
    { id: "custom", kind: "openai_compatible", name: "本机兼容服务", baseUrl: "http://127.0.0.1:8080/v1" },
  ];
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [routeHealth, setRouteHealth] = useState<RouteHealthRecord[]>([]);
  const [status, setStatus] = useState<Record<string, ProviderStatus>>({});
  const [editing, setEditing] = useState(false);
  const [wizardStep, setWizardStep] = useState<"preset" | "configuration">("preset");
  const [viewingProviderId, setViewingProviderId] = useState<string | null>(null);
  const [defaultProviderDraft, setDefaultProviderDraft] = useState<string | null>(null);
  const [presetId, setPresetId] = useState<ProviderPresetId>("ollama");
  const [name, setName] = useState("Ollama");
  const [kind, setKind] = useState<ProviderKind>("ollama");
  const [baseUrl, setBaseUrl] = useState("http://127.0.0.1:11434");
  const [apiKey, setApiKey] = useState("");
  const [customProtocol, setCustomProtocol] = useState<"openai_chat" | "openai_responses" | "anthropic_messages">("openai_chat");
  const [error, setError] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [confirmationBusy, setConfirmationBusy] = useState(false);
  const catalogTrigger = useRef<HTMLButtonElement | null>(null);
  const saveLock = useRef(false);
  const wizardRevision = useRef(0);
  const providerDraftId = useRef<string | null>(null);
  const confirmationLock = useRef(false);
  const [savingProvider, setSavingProvider] = useState(false);

  const loadSettings = useCallback(async () => {
    setError(null);
    try { setSettings(await settingsRepository.getAppSettings()); }
    catch (value) { setError(String(value)); }
  }, []);
  useEffect(() => {
    void loadSettings();
    void modelCatalogRepository.listRouteHealth().then(setRouteHealth).catch(() => setRouteHealth([]));
  }, [loadSettings, coreOnline]);

  const modelSettings = settings?.models;
  const viewingProvider = modelSettings?.providers.find((provider) => provider.id === viewingProviderId);

  const closeCatalog = useCallback(() => {
    setViewingProviderId(null);
    window.requestAnimationFrame(() => catalogTrigger.current?.focus());
  }, []);

  const refreshRouteHealth = useCallback(async () => {
    try { setRouteHealth(await modelCatalogRepository.listRouteHealth()); }
    catch { setRouteHealth([]); }
  }, []);

  function choosePreset(preset: Preset) {
    setPresetId(preset.id);
    setName(preset.name);
    setKind(preset.kind);
    setBaseUrl(preset.baseUrl);
    setWizardStep("configuration");
  }

  function closeWizard() {
    wizardRevision.current += 1;
    providerDraftId.current = null;
    setEditing(false);
    setWizardStep("preset");
    setApiKey("");
  }

  async function refreshModels(providerId: string) {
    setStatus((current) => ({ ...current, [providerId]: "loading" }));
    setError(null);
    try {
      const updated = await providerRepository.refreshModels(providerId);
      setSettings(updated);
      setStatus((current) => ({ ...current, [providerId]: "ok" }));
      return true;
    } catch (value) {
      setStatus((current) => ({ ...current, [providerId]: "error" }));
      setError(String(value));
      return false;
    }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (saveLock.current) return;
    saveLock.current = true; setSavingProvider(true);
    const revision = wizardRevision.current;
    const id = providerDraftId.current ??= crypto.randomUUID();
    try {
      const updated = await providerRepository.save({
        id, name: name.trim(), kind, preset_id: presetId,
        entered_base_url: baseUrl.trim(), enabled: false,
        api_key: apiKey || undefined,
        primary_protocol: presetId === "custom" ? customProtocol : undefined,
      });
      setSettings(updated);
      if (revision === wizardRevision.current) {
        closeWizard();
        setViewingProviderId(id);
      }
      await refreshModels(id);
    } catch (value) {
      setError(String(value));
    } finally { saveLock.current = false; setSavingProvider(false); }
  }

  async function requestRemove(providerId: string, providerName: string) {
    try {
      const affected = await providerRepository.countReferences(providerId);
      setConfirmation({ kind: "delete-provider", providerId, providerName, count: affected });
    } catch (value) { setError(String(value)); }
  }

  async function setModelEnabled(providerId: string, modelId: string, enabled: boolean) {
    try {
      let confirmed = false;
      if (!enabled) {
        const affected = await modelCatalogRepository.countReferences(providerId, modelId);
        if (affected > 0) {
          setConfirmation({ kind: "disable-model", providerId, modelId, count: affected });
          return;
        }
      }
      setSettings(await modelCatalogRepository.setEnabled(providerId, modelId, enabled, confirmed));
      setError(null);
    } catch (value) { setError(String(value)); }
  }

  async function setModelAutomaticRouting(providerId: string, modelId: string, allowed: boolean) {
    try {
      if (!allowed) {
        const affected = await modelCatalogRepository.countReferences(providerId, modelId);
        if (affected > 0) {
          setConfirmation({ kind: "disable-automatic", providerId, modelId, count: affected });
          return;
        }
      }
      setSettings(await modelCatalogRepository.setAutomaticRouting(providerId, modelId, allowed));
      setError(null);
    } catch (value) { setError(String(value)); }
  }

  async function addManualModel(providerId: string, modelId: string) {
    try {
      setSettings(await modelCatalogRepository.addManual(providerId, modelId));
      setError(null);
    } catch (value) { setError(String(value)); throw value; }
  }

  async function removeManualModel(providerId: string, modelId: string) {
    try {
      const affected = await modelCatalogRepository.countReferences(providerId, modelId);
      if (affected > 0) {
        setConfirmation({ kind: "remove-manual", providerId, modelId, count: affected });
        return;
      }
      setError(await applyManualRemoval(providerId, modelId));
    } catch (value) { setError(String(value)); }
  }

  async function applyManualRemoval(providerId: string, modelId: string, confirmed = false) {
    try {
      setSettings(await modelCatalogRepository.removeManual(providerId, modelId, confirmed));
      return null;
    } catch (value) {
      // Persistence can succeed before runtime reconfiguration fails. Reflect
      // authoritative settings rather than leaving a deleted model to retry.
      let current: AppSettings | null = null;
      try { current = await settingsRepository.getAppSettings(); } catch { /* Keep the operation error. */ }
      if (current) {
        setSettings(current);
        const provider = current.models.providers.find(item => item.id === providerId);
        if (provider && !provider.models.some(item => item.id === modelId)) return t("models.routingSyncPending");
      }
      throw value;
    }
  }

  async function setProviderEnabled(providerId: string, providerName: string, enabled: boolean) {
    try {
      let confirmed = false;
      if (!enabled) {
        const affected = await providerRepository.countReferences(providerId);
        if (affected > 0) {
          setConfirmation({ kind: "disable-provider", providerId, providerName, count: affected });
          return;
        }
      }
      setSettings(await providerRepository.setEnabled(providerId, enabled, confirmed));
      setError(null);
    } catch (value) { setError(String(value)); }
  }

  async function setDefault(providerId: string, modelId: string) {
    if (!modelId) return;
    try {
      const result = await modelCatalogRepository.updateDefault(providerId, modelId);
      setSettings(result.settings);
      setDefaultProviderDraft(null);
      setError(result.core_sync === "pending" ? t("models.routingSyncPending") : null);
    } catch (value) { setError(String(value)); }
  }

  async function enableWebSearchPrivacy() {
    if (!settings) return;
    try {
      setSettings(await settingsRepository.updateWebSearch({
        enabled: true,
        privacy_acknowledged: true,
      }));
      setError(null);
    } catch (value) { setError(String(value)); }
  }

  async function probeTools(providerId: string, modelId: string) {
    try {
      setSettings(await modelCatalogRepository.probeTools(providerId, modelId));
      setError(null);
    } catch (value) {
      try { setSettings(await settingsRepository.getAppSettings()); } catch { /* Keep the last known settings. */ }
      setError(String(value));
    }
  }

  async function probeNativeSearch(providerId: string, modelId: string) {
    try {
      setSettings(await webSearchRepository.probeNative(providerId, modelId));
      await refreshRouteHealth();
      setError(null);
    } catch (value) {
      await refreshRouteHealth();
      try { setSettings(await settingsRepository.getAppSettings()); } catch { /* Preserve the last known settings. */ }
      setError(String(value));
    }
  }

  async function updateContextLimits(providerId: string, modelId: string, contextLength: number | null, maxOutputTokens: number | null) {
    try {
      setSettings(await modelCatalogRepository.updateContextLimits(
        providerId, modelId, contextLength, maxOutputTokens,
      ));
      setError(null);
    } catch (value) {
      setError(String(value));
      throw value;
    }
  }

  async function updateRoutingMetadata(providerId: string, modelId: string, inputPrice: string | null,
    outputPrice: string | null, qualityScore: number | null, priority: number) {
    try {
      setSettings(await modelCatalogRepository.updateRoutingMetadata(
        providerId, modelId, inputPrice, outputPrice, qualityScore, priority,
      ));
      setError(null);
    } catch (value) {
      setError(String(value));
      throw value;
    }
  }

  async function confirmPendingAction() {
    if (!confirmation || confirmationLock.current) return;
    confirmationLock.current = true;
    setConfirmationBusy(true);
    try {
      let warning: string | null = null;
      if (confirmation.kind === "remove-manual") {
        warning = await applyManualRemoval(confirmation.providerId, confirmation.modelId, true);
      } else if (confirmation.kind === "disable-model") {
        setSettings(await modelCatalogRepository.setEnabled(
          confirmation.providerId, confirmation.modelId, false, true,
        ));
      } else if (confirmation.kind === "disable-automatic") {
        setSettings(await modelCatalogRepository.setAutomaticRouting(
          confirmation.providerId, confirmation.modelId, false, true,
        ));
      } else if (confirmation.kind === "disable-provider") {
        setSettings(await providerRepository.setEnabled(confirmation.providerId, false, true));
      } else {
        setSettings(await providerRepository.delete(confirmation.providerId, confirmation.count > 0));
        closeCatalog();
      }
      setConfirmation(null);
      setError(warning);
    } catch (value) {
      setError(String(value));
      setConfirmation(null);
    } finally {
      confirmationLock.current = false;
      setConfirmationBusy(false);
    }
  }

  function confirmationCopy() {
    if (!confirmation) return { title: "", description: "", confirmLabel: "" };
    if (confirmation.kind === "remove-manual") return {
      title: t("buttonAudit.removeModelTitle", { model: confirmation.modelId }),
      description: t("buttonAudit.removeModelDescription", { count: confirmation.count }),
      confirmLabel: t("models.removeManual"),
    };
    if (confirmation.kind === "disable-model") return {
      title: t("models.confirm.disableModelTitle", { model: confirmation.modelId }),
      description: t("models.disableReferencedConfirm", { count: confirmation.count }),
      confirmLabel: t("models.confirm.disable"),
    };
    if (confirmation.kind === "disable-automatic") return {
      title: t("models.confirm.disableAutomaticTitle", { model: confirmation.modelId }),
      description: t("models.disableAutomaticReferencedConfirm", { count: confirmation.count }),
      confirmLabel: t("models.confirm.disable"),
    };
    if (confirmation.kind === "disable-provider") return {
      title: t("models.confirm.disableProviderTitle", { provider: confirmation.providerName }),
      description: t("models.disableProviderReferencedConfirm", { count: confirmation.count }),
      confirmLabel: t("models.confirm.disable"),
    };
    return {
      title: t("models.confirm.deleteProviderTitle", { provider: confirmation.providerName }),
      description: confirmation.count > 0
        ? t("models.deleteProviderReferencedConfirm", { count: confirmation.count })
        : t("models.confirm.deleteProviderDescription"),
      confirmLabel: t("models.delete"),
    };
  }

  if (!settings || !modelSettings) return <div className="state-message"><p>{error ?? t("models.loading")}</p>
    {error ? <Button onClick={() => void loadSettings()}>{t("actions.retry")}</Button> : null}
  </div>;

  const enabledProviders = modelSettings.providers.filter((provider) => provider.enabled && provider.models.some((model) => model.enabled && (model.status !== "unavailable" || provider.id.startsWith("managed:")) && model.capabilities.text !== "no"));
  const selectedDefaultProviderId = defaultProviderDraft ?? modelSettings.default_provider_profile_id;
  const selectedDefaultProvider = enabledProviders.find((provider) => provider.id === selectedDefaultProviderId) ?? enabledProviders[0];
  const selectedProviderModels = selectedDefaultProvider?.models.filter((model) => model.enabled && (model.status !== "unavailable" || selectedDefaultProvider.id.startsWith("managed:")) && model.capabilities.text !== "no") ?? [];
  const routing = settings.routing ?? {
    new_conversation_mode: "automatic" as const, default_preset: "balanced" as const,
    max_model_calls_per_message: 3, max_specialist_steps: 2,
    max_extra_online_cost_usd: "0.20", allow_unknown_price_automatic: false,
  };
  const updateRouting = async (next: Partial<RoutingPreferences>) => {
    try { setSettings(await settingsRepository.updateRoutingPreferences(next)); setError(null); }
    catch (value) { setError(String(value)); }
  };

  return (
    <div className="page-content models-page">
      <header className="page-heading models-page__heading">
        <div><span className="models-page__heading-icon"><Bot size={26} /></span><div><h1>本地模型</h1><p>{t("pages.models.description")}</p></div></div>
        <Button icon={<Plus size={16} />} variant="primary" onClick={() => setEditing(true)}>{t("models.add")}</Button>
      </header>

      {error && !editing && !viewingProvider ? <div className="models-alert" role="alert"><XCircle size={18} /><span>{error}</span><button type="button" onClick={() => setError(null)} aria-label={t("actions.close")}><X size={16} /></button></div> : null}

      <LocalModelsPanel onChanged={loadSettings} onAuxiliary={() => { window.sessionStorage.setItem("agent:rag-tab", "models"); setCurrentPage("rag"); }} />

      <Card className="models-default-card">
        <div className="models-default-card__copy"><span className="models-default-card__icon"><Bot size={21} /></span><div><h2>{t("models.defaultModel")}</h2><p>{t("models.defaultDescription")}</p></div></div>
        <div className="models-default-card__controls">
          <label><span>{t("models.provider")}</span><select value={selectedDefaultProvider?.id ?? ""} onChange={(event) => setDefaultProviderDraft(event.target.value)}>
            {enabledProviders.map((provider) => <option key={provider.id} value={provider.id}>{provider.name}</option>)}
          </select></label>
          <label><span>{t("models.model")}</span><select value={modelSettings.default_provider_profile_id === selectedDefaultProvider?.id ? modelSettings.default_model_id : ""} disabled={!selectedDefaultProvider} onChange={(event) => selectedDefaultProvider && void setDefault(selectedDefaultProvider.id, event.target.value)}>
            <option value="">{t("models.chooseDefault")}</option>{selectedProviderModels.map((model) => <option value={model.id} key={model.id}>{model.id}</option>)}
          </select></label>
        </div>
      </Card>

      <RoutingSettingsCard value={routing} onChange={updateRouting} />

      <section className="models-section" aria-labelledby="model-services-heading">
        <div className="models-section__heading"><div><h2 id="model-services-heading">{t("models.services")}</h2><p>{t("models.servicesDescription")}</p></div><span>{t("models.serviceCount", { count: modelSettings.providers.length })}</span></div>
        <div className="model-grid">
          {modelSettings.providers.filter(provider => !provider.id.startsWith("managed:")).map((provider) => {
            const providerStatus = status[provider.id] ?? "idle";
            const offlineDisabled = !isLocalProvider(provider);
            const isDefault = modelSettings.default_provider_profile_id === provider.id;
            const enabledModels = provider.models.filter((model) => model.enabled && model.status !== "unavailable" && model.capabilities.text !== "no");
            const automaticModels = enabledModels.filter((model) => model.allow_automatic_routing);
            const nativeModels = enabledModels.filter((model) => ["declared", "verified"].includes(
              nativeSearchState(provider, model)?.support_status
                ?? model.native_web_search_status
                ?? "unknown",
            ));
            const address = provider.resolved_api_base_url ?? provider.entered_base_url;
            return <Card className="model-provider-card" key={provider.id}>
              <div className="model-provider-card__header">
                <span className={`model-provider-card__icon model-provider-card__icon--${provider.kind}`}>{providerIcon(provider.kind)}</span>
                <div className="model-provider-card__identity"><div><h3>{provider.name}</h3>{offlineDisabled && <span>离线模式已停用</span>}{isDefault ? <span className="model-badge model-badge--primary">{t("models.currentDefault")}</span> : null}</div><span>{t(`models.providerKinds.${provider.kind}`)}</span></div>
                <span className={`model-status model-status--${providerStatus === "idle" && !provider.enabled ? "error" : providerStatus}`}>
                  {providerStatus === "loading" ? <LoaderCircle size={13} /> : providerStatus === "ok" ? <CheckCircle2 size={13} /> : providerStatus === "error" || !provider.enabled ? <XCircle size={13} /> : <span />}
                  {provider.enabled ? t(`models.status.${providerStatus}`) : t("models.status.disabled")}
                </span>
              </div>
              <div className="model-provider-card__details">
                <div className="model-detail-row"><Server size={15} /><div><span>{t("models.endpoint")}</span><strong title={address}>{address}</strong></div></div>
                {provider.kind === "openai_compatible" ? <div className="model-detail-row"><KeyRound size={15} /><div><span>{t("models.credential")}</span><strong>{provider.credential_ref ? t("models.keyStored") : t("models.noKey")}</strong></div></div> : null}
              </div>
              <div className="model-summary"><div><span>{t("models.availableModels")}</span><small>{t("models.modelCounts", { discovered: provider.models.length, enabled: enabledModels.length })}</small><small>{t("models.automaticModelCount", { count: automaticModels.length })}</small><small>{t("models.nativeSearchCount", { count: nativeModels.length })}</small><small>{t(settings.web_search?.default_backend_id ? "models.externalSearchConfigured" : "models.externalSearchNotConfigured")}</small></div>
                <div className="model-summary__chips">{enabledModels.slice(0, 3).map((model) => <span title={model.id} key={model.id}>{model.id}</span>)}{enabledModels.length > 3 ? <span>+{enabledModels.length - 3}</span> : null}</div>
              </div>
              <div className="model-provider-actions">
                <Button icon={<List size={15} />} onClick={(event) => { catalogTrigger.current = event.currentTarget; setViewingProviderId(provider.id); }}>{t("models.manageModels")}</Button>
                <Button icon={providerStatus === "loading" ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />} onClick={() => void refreshModels(provider.id)} disabled={offlineDisabled || providerStatus === "loading"}>{providerStatus === "loading" ? t("models.testing") : t("models.test")}</Button>
                {provider.id !== "builtin-demo" ? <Button icon={<Power size={15} />} variant="ghost" disabled={offlineDisabled || (isDefault && provider.enabled)} onClick={() => void setProviderEnabled(provider.id, provider.name, !provider.enabled)}>{provider.enabled ? t("models.disableProvider") : t("models.enableProvider")}</Button> : null}
                {provider.id !== "builtin-demo" ? <Button className="model-delete-button" icon={<Trash2 size={15} />} variant="ghost" disabled={isDefault} onClick={() => void requestRemove(provider.id, provider.name)}>{t("models.delete")}</Button> : null}
              </div>
            </Card>;
          })}
        </div>
      </section>

      {viewingProvider ? <ModelCatalogDrawer key={viewingProvider.id} provider={viewingProvider}
        error={error}
        routeHealth={routeHealth.filter((record) => record.provider_or_backend_id === viewingProvider.id)}
        defaultProviderId={modelSettings.default_provider_profile_id} defaultModelId={modelSettings.default_model_id}
        onClose={closeCatalog}
        onEnabledChange={(modelId, enabled) => void setModelEnabled(viewingProvider.id, modelId, enabled)}
        onAutomaticRoutingChange={(modelId, allowed) => void setModelAutomaticRouting(viewingProvider.id, modelId, allowed)}
        onProbeTools={(modelId) => void probeTools(viewingProvider.id, modelId)}
        onProbeNativeSearch={(modelId) => void probeNativeSearch(viewingProvider.id, modelId)}
        onResetRouteHealth={async (key) => {
          try { setRouteHealth(await modelCatalogRepository.resetRouteHealth(key)); }
          catch (value) { setError(String(value)); }
        }}
        onConfigureExternalSearch={() => {
          window.sessionStorage.setItem("agent:settings-section", "web-search");
          closeCatalog();
          setCurrentPage("settings");
        }}
        webPrivacyAcknowledged={Boolean(settings.web_search?.enabled && settings.web_search.privacy_acknowledged)}
        onEnableWebSearch={() => void enableWebSearchPrivacy()}
        onSetDefault={(modelId) => void setDefault(viewingProvider.id, modelId)}
        onRemoveManual={(modelId) => void removeManualModel(viewingProvider.id, modelId)}
        onResetVision={(modelId) => void settingsRepository
          .updateModelVisionConfirmation(viewingProvider.id, modelId, null)
          .then(setSettings).catch((value) => setError(String(value)))}
        onSaveLimits={(modelId, contextLength, maxOutputTokens) => updateContextLimits(viewingProvider.id, modelId, contextLength, maxOutputTokens)}
        onSaveRouting={(modelId, inputPrice, outputPrice, qualityScore, priority) =>
          updateRoutingMetadata(viewingProvider.id, modelId, inputPrice, outputPrice, qualityScore, priority)}
        onAddManual={(modelId) => addManualModel(viewingProvider.id, modelId)} /> : null}

      {editing ? <div className="drawer-backdrop" onMouseDown={closeWizard}><aside className="project-drawer model-provider-drawer" role="dialog" aria-modal="true" aria-labelledby="add-model-provider-title" onMouseDown={(event) => event.stopPropagation()}>
        <div className="drawer-header"><div><span className="model-provider-drawer__icon"><Plus size={20} /></span><div><h2 id="add-model-provider-title">{t("models.add")}</h2><p>{t("models.addDescription")}</p></div></div><button type="button" onClick={closeWizard} aria-label={t("actions.close")}><X size={19} /></button></div>
        {error ? <p role="alert">{error}</p> : null}
        {wizardStep === "preset" ? <div className="provider-preset-list">{presets.map((preset) => <button type="button" key={preset.id} onClick={() => choosePreset(preset)}><span className={`model-provider-card__icon model-provider-card__icon--${preset.kind}`}>{providerIcon(preset.kind)}</span><span><strong>{preset.name}</strong><small>{preset.id === "ollama" ? t("models.localService") : preset.id === "custom" ? t("models.customService") : t("models.onlineService")}</small></span></button>)}</div> : <form onSubmit={(event) => void save(event)}>
          <label>{t("models.name")}<input disabled={savingProvider} required maxLength={80} autoFocus value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label className={presetId === "custom" ? "" : "advanced-field"}>{t("models.baseUrl")}<input disabled={savingProvider} required readOnly={presetId !== "custom"} value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} /><small>{presetId === "custom" ? t("models.apiBaseUrlHint") : t("models.presetUrlHint")}</small></label>
          {kind === "openai_compatible" ? <label>{t("models.apiKey")}<input disabled={savingProvider} type="password" autoComplete="new-password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} /><small>{t("models.apiKeyHint")}</small></label> : null}
          {presetId === "custom" ? <label>{t("models.apiProtocol")}<select disabled={savingProvider} value={customProtocol}
            onChange={(event) => setCustomProtocol(event.target.value as typeof customProtocol)}>
            <option value="openai_chat">OpenAI Chat Completions</option>
            <option value="openai_responses">OpenAI Responses</option>
            <option value="anthropic_messages">Anthropic Messages</option>
          </select><small>{t("models.apiProtocolHint")}</small></label> : null}
          <div className="drawer-actions"><Button type="button" variant="ghost" disabled={savingProvider} onClick={() => setWizardStep("preset")}>{t("actions.back")}</Button><Button type="submit" variant="primary" disabled={savingProvider || !name.trim() || !baseUrl.trim()}>{savingProvider ? t("actions.saving") : t("models.testAndContinue")}</Button></div>
        </form>}
      </aside></div> : null}
      <ConfirmDialog open={confirmation !== null} {...confirmationCopy()} busy={confirmationBusy} tone="danger"
        onCancel={() => setConfirmation(null)} onConfirm={() => void confirmPendingAction()} />
    </div>
  );
}
