import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../../contexts/AppContext";
import { settingsRepository, webSearchRepository } from "../../services/runtime";
import type { Conversation, Project, WebSearchRequestMode } from "../../types/domain";
import { nativeSearchRoute, nativeSearchState, type AppSettings } from "../../types/settings";

export function useWebSearchController(conversation: Conversation | null, project: Project | undefined) {
  const { currentPage, draft } = useApp();
  const scope = conversation ? `conversation:${conversation.id}:${project?.id ?? ""}`
    : `draft:${draft.revision}:${draft.kind}:${project?.id ?? draft.projectId ?? ""}`;
  const scopeRef = useRef(scope); scopeRef.current = scope;
  const pendingScopes = useRef(new Set<string>());
  const settingsRevision = useRef(0);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [draftMode, setDraftMode] = useState<"inherit" | "off" | "auto">("inherit");
  const [forceOnce, setForceOnce] = useState(false);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [projectConsent, setProjectConsent] = useState(false);
  const [connectionRevision, setConnectionRevision] = useState(0);
  const [permissionRevision, setPermissionRevision] = useState(0);

  const reloadSettings = useCallback(async () => {
    const revision = ++settingsRevision.current;
    try {
      const value = await settingsRepository.getAppSettings();
      if (revision === settingsRevision.current) { setSettings(value); setError(current => current === "settings" ? null : current); }
    } catch { if (revision === settingsRevision.current) setError("settings"); }
  }, []);
  useEffect(() => {
    if (currentPage === "chat") void reloadSettings();
    return () => { settingsRevision.current += 1; };
  }, [currentPage, open, scope, reloadSettings]);
  useEffect(() => {
    setDraftMode(conversation?.web_search_mode ?? "inherit");
    setForceOnce(false);
    setOpen(false);
    setProjectConsent(false);
    setBusy(pendingScopes.current.has(scope));
    setError(null);
  }, [scope, conversation?.web_search_mode]);

  const isProjectWork = (conversation?.kind ?? (project ? "project_work" : "standard_chat")) === "project_work";
  const projectAllowed = !isProjectWork || projectConsent || project?.web_access_policy === "allow_search";
  const webSettings = settings?.web_search;
  const hasBackend = Boolean(webSettings?.default_backend_id
    && webSettings.backends.some((item) => item.id === webSettings.default_backend_id && item.enabled));
  const selectedProviderId = conversation?.model_selection_mode === "automatic"
    ? conversation.coordinator_provider_profile_id ?? settings?.models.default_provider_profile_id
    : conversation?.provider_profile_id ?? settings?.models.default_provider_profile_id;
  const selectedModelId = conversation?.model_selection_mode === "automatic"
    ? conversation.coordinator_model_id ?? settings?.models.default_model_id
    : conversation?.model_id ?? settings?.models.default_model_id;
  const selectedProvider = settings?.models.providers.find((item) => item.id === selectedProviderId);
  const selectedModel = selectedProvider?.models.find((item) => item.id === selectedModelId);
  const hasNativeSearch = selectedProvider?.enabled === true
    && Boolean(nativeSearchRoute(selectedProvider))
    && selectedModel?.enabled === true
    && selectedModel.status !== "unavailable"
    && ["declared", "verified"].includes(
      nativeSearchState(selectedProvider, selectedModel)?.support_status
        ?? selectedModel.native_web_search_status
        ?? "unknown",
    );
  const hasSearchPath = hasNativeSearch || hasBackend;
  const defaultBackend = webSettings?.backends.find(
    (item) => item.id === webSettings.default_backend_id && item.enabled,
  );
  const currentPath = hasNativeSearch
    ? { kind: "native" as const, label: selectedProvider?.name ?? "" }
    : defaultBackend
      ? { kind: "backend" as const, label: defaultBackend.name }
      : null;
  const globalEnabled = Boolean(webSettings?.enabled && webSettings.privacy_acknowledged);
  const available = globalEnabled && projectAllowed && hasSearchPath;
  // draftMode is initialized from the conversation and then owns the local UI state.
  // Reading conversation.web_search_mode here made a successful click look inert,
  // because ChatPage does not replace its conversation object after IPC.
  const mode = draftMode;
  const resolvedMode = mode === "inherit" ? (globalEnabled ? "auto" : "off") : mode;
  const requestMode: WebSearchRequestMode = forceOnce ? "force_once" : resolvedMode;
  const disabledReason = !settings ? "loading" : !globalEnabled ? "global" : !projectAllowed ? "project" : !hasSearchPath ? "backend" : null;

  const enableGlobal = useCallback(async () => {
    if (pendingScopes.current.has(scope)) return;
    pendingScopes.current.add(scope);
    settingsRevision.current += 1;
    setBusy(true); setError(null);
    try {
      const updated = await settingsRepository.updateWebSearch({
        enabled: true, privacy_acknowledged: true,
      });
      if (scopeRef.current === scope) { settingsRevision.current += 1; setSettings(updated); }
    } catch { if (scopeRef.current === scope) setError("save"); }
    finally { pendingScopes.current.delete(scope); if (scopeRef.current === scope) setBusy(false); }
  }, [scope]);

  const setMode = useCallback(async (next: "inherit" | "off" | "auto") => {
    if (pendingScopes.current.has(scope)) return;
    const previousMode = draftMode;
    const previousForceOnce = forceOnce;
    setError(null);
    setDraftMode(next);
    setForceOnce(false);
    setOpen(false);
    if (conversation) {
      pendingScopes.current.add(scope);
      setBusy(true);
      try { await webSearchRepository.updateConversation(conversation.id, next); }
      catch {
        if (scopeRef.current !== scope) return;
        setDraftMode(previousMode);
        setForceOnce(previousForceOnce);
        setOpen(true);
        setError("save");
        return;
      }
      finally { pendingScopes.current.delete(scope); if (scopeRef.current === scope) setBusy(false); }
    }
  }, [scope, conversation, draftMode, forceOnce]);

  const allowProject = useCallback(async () => {
    if (!project || pendingScopes.current.has(scope)) return;
    pendingScopes.current.add(scope);
    setBusy(true); setError(null);
    try {
      await webSearchRepository.updateProject(project.id, "allow_search");
      if (scopeRef.current !== scope) return;
      setProjectConsent(true);
      setPermissionRevision((value) => value + 1);
    }
    catch { if (scopeRef.current === scope) setError("save"); }
    finally { pendingScopes.current.delete(scope); if (scopeRef.current === scope) setBusy(false); }
  }, [scope, project]);

  const connectTavily = useCallback(async (apiKey: string) => {
    if (pendingScopes.current.has(scope)) return;
    pendingScopes.current.add(scope); settingsRevision.current += 1;
    setBusy(true); setError(null);
    try {
      await webSearchRepository.saveBackend({
        id: "tavily-default", name: "Tavily", kind: "tavily",
        baseUrl: "https://api.tavily.com", enabled: true, apiKey,
      });
      await webSearchRepository.testBackend("tavily-default");
      const updated = await settingsRepository.updateWebSearch({
        enabled: true, privacy_acknowledged: true,
        default_backend_id: "tavily-default",
      });
      if (scopeRef.current !== scope) return;
      settingsRevision.current += 1; setSettings(updated);
      setConnectionRevision((value) => value + 1);
    } catch {
      if (scopeRef.current === scope) setError("backend");
    } finally { pendingScopes.current.delete(scope); if (scopeRef.current === scope) setBusy(false); }
  }, [scope]);

  return useMemo(() => ({
    settings, open, setOpen, busy, error, reloadSettings, available, globalEnabled, projectAllowed,
    isProjectWork,
    connectionRevision, permissionRevision,
    hasBackend, hasNativeSearch, hasSearchPath, currentPath, disabledReason,
    mode, forceOnce, requestMode, setMode,
    enableGlobal, allowProject, connectTavily, forceSearchOnce: () => setForceOnce(true),
    clearAcceptedOneShot: () => setForceOnce(false),
  }), [settings, open, busy, error, reloadSettings, available, globalEnabled, projectAllowed, connectionRevision,
    permissionRevision, hasBackend,
    hasNativeSearch, hasSearchPath, currentPath, isProjectWork,
    disabledReason, mode, forceOnce, requestMode, setMode, enableGlobal, allowProject, connectTavily]);
}

export type WebSearchController = ReturnType<typeof useWebSearchController>;
