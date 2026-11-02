import { ArrowLeft, CheckCircle2, Globe2, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { settingsRepository, webSearchRepository } from "../../services/runtime";
import type { AppSettings } from "../../types/settings";

export function WebSearchSettingsPage({ onBack }: { onBack(): void }) {
  const { t } = useTranslation("common");
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"tavily" | "searxng">("tavily");
  const [url, setUrl] = useState("https://api.tavily.com");
  const [key, setKey] = useState("");
  const [testing, setTesting] = useState<string | null>(null);
  const operationLock = useRef(false);
  const revision = useRef(0);
  const load = useCallback(async () => {
    const current = ++revision.current;
    setBusy(true); setError(null);
    try { const updated = await settingsRepository.getAppSettings(); if (current === revision.current) setSettings(updated); }
    catch { if (current === revision.current) setError("load"); }
    finally { if (current === revision.current) setBusy(false); }
  }, []);

  useEffect(() => { void load(); return () => { revision.current += 1; }; }, [load]);
  async function update(patch: Partial<NonNullable<typeof settings>["web_search"]>) {
    if (!settings || operationLock.current) return;
    operationLock.current = true;
    setBusy(true); setError(null);
    try { setSettings(await webSearchRepository.updateSettings(patch)); }
    catch { setError("save"); } finally { operationLock.current = false; setBusy(false); }
  }
  async function add(event: FormEvent) {
    event.preventDefault();
    if (operationLock.current || !settings) return;
    operationLock.current = true; setBusy(true); setError(null);
    try {
      const id = crypto.randomUUID();
      const updated = await webSearchRepository.saveBackend({ id, name: name.trim(), kind, baseUrl: url.trim(), enabled: true, apiKey: kind === "tavily" ? key : undefined });
      setSettings(updated);
      setAdding(false); setName(""); setKey("");
    } catch { setError("save"); } finally { operationLock.current = false; setBusy(false); }
  }
  async function test(id: string) {
    if (operationLock.current) return;
    operationLock.current = true; setBusy(true); setTesting(id); setError(null);
    try { await webSearchRepository.testBackend(id); }
    catch { setError("test"); }
    finally {
      try { setSettings(await settingsRepository.getAppSettings()); } catch { /* keep the last valid settings */ }
      setTesting(null);
      operationLock.current = false; setBusy(false);
    }
  }
  async function remove(id: string) {
    if (operationLock.current) return;
    operationLock.current = true; setBusy(true); setError(null);
    try { setSettings(await webSearchRepository.deleteBackend(id)); }
    catch { setError("save"); } finally { operationLock.current = false; setBusy(false); }
  }
  async function toggleBackend(backend: NonNullable<typeof settings>["web_search"]["backends"][number]) {
    if (operationLock.current) return;
    operationLock.current = true;
    setBusy(true); setError(null);
    try {
      setSettings(await webSearchRepository.saveBackend({
        id: backend.id, name: backend.name, kind: backend.kind,
        baseUrl: backend.base_url, enabled: !backend.enabled,
      }));
    } catch { setError("save"); } finally { operationLock.current = false; setBusy(false); }
  }

  return <div className="settings-shell web-search-settings-page">
    <div className="page-heading-row">
      <div className="page-intro"><button className="text-action" onClick={onBack}><ArrowLeft size={14} />{t("actions.back")}</button>
        <h2>{t("settings.webSearch.title")}</h2><p>{t("settings.webSearch.description")}</p></div>
      <Button icon={<Plus size={16} />} disabled={!settings || busy} onClick={() => setAdding(!adding)}>{t("settings.webSearch.addBackend")}</Button>
    </div>
    <Card className="web-search-master-card">
      <div className="settings-language-card__icon"><Globe2 size={24} /></div>
      <div><strong>{t("settings.webSearch.master")}</strong><p>{t("settings.webSearch.privacy")}</p></div>
      <label className="settings-switch"><input type="checkbox" checked={settings?.web_search.enabled ?? false} disabled={!settings || busy}
        onChange={(event) => void update({ enabled: event.target.checked, privacy_acknowledged: event.target.checked || settings?.web_search.privacy_acknowledged })} />
        <span>{settings?.web_search.enabled ? t("settings.webSearch.enabled") : t("settings.webSearch.disabled")}</span></label>
    </Card>
    <Card><div className="web-search-preferences">
      <label className="settings-switch"><input type="checkbox"
        checked={settings?.web_search.safe_search ?? true} disabled={!settings || busy}
        onChange={(event) => void update({ safe_search: event.target.checked })} />
        <span>{t("settings.webSearch.safeSearch")}</span></label>
      <label>{t("settings.webSearch.language")}<input key={`language-${settings?.web_search.default_language}`}
        defaultValue={settings?.web_search.default_language ?? "auto"} disabled={!settings || busy}
        onBlur={(event) => void update({ default_language: event.target.value.trim() || "auto" })} /></label>
      <label>{t("settings.webSearch.region")}<input key={`region-${settings?.web_search.default_region}`}
        defaultValue={settings?.web_search.default_region ?? "auto"} disabled={!settings || busy}
        onBlur={(event) => void update({ default_region: event.target.value.trim() || "auto" })} /></label>
    </div></Card>
    {adding ? <Card><form className="web-search-backend-form" onSubmit={(event) => void add(event)}>
      <label>{t("settings.webSearch.backendName")}<input required disabled={busy} value={name} onChange={(event) => setName(event.target.value)} /></label>
      <label>{t("settings.webSearch.backendType")}<select disabled={busy} value={kind} onChange={(event) => {
        const next = event.target.value as "tavily" | "searxng"; setKind(next); setUrl(next === "tavily" ? "https://api.tavily.com" : ""); setKey("");
      }}><option value="tavily">Tavily</option><option value="searxng">SearXNG</option></select></label>
      <label>{t("settings.webSearch.baseUrl")}<input disabled={busy} required type="url" value={url} onChange={(event) => setUrl(event.target.value)} /></label>
      {kind === "tavily" ? <label>{t("settings.webSearch.apiKey")}<input disabled={busy} required type="password" value={key} onChange={(event) => setKey(event.target.value)} /></label> : null}
      <div><Button type="button" disabled={busy} onClick={() => setAdding(false)}>{t("actions.cancel")}</Button><Button type="submit" variant="primary" disabled={busy}>{t("actions.save")}</Button></div>
    </form></Card> : null}
    <div className="web-search-backend-list">
      {settings?.web_search.backends.map((backend) => <Card key={backend.id} className="web-search-backend-card">
        <div><strong>{backend.name}</strong><span>{backend.kind === "tavily" ? "Tavily" : "SearXNG"}</span><small>{backend.base_url}</small>
          {backend.last_status ? <small className={`web-search-health web-search-health--${backend.last_status === "available" ? "ok" : "error"}`}>
            {backend.last_status === "available" ? t("settings.webSearch.healthAvailable") : t("settings.webSearch.healthUnavailable")}
            {backend.last_tested_at ? ` · ${new Date(backend.last_tested_at).toLocaleString()}` : ""}
          </small> : <small className="web-search-health">{t("settings.webSearch.healthUntested")}</small>}
        </div>
        {settings.web_search.default_backend_id === backend.id ? <span className="status-chip"><CheckCircle2 size={13} />{t("settings.webSearch.default")}</span> : null}
        <div className="web-search-backend-card__actions">
          <Button icon={<RefreshCw size={14} />} disabled={busy || testing !== null} onClick={() => void test(backend.id)}>{t("settings.webSearch.test")}</Button>
          <Button disabled={busy} onClick={() => void toggleBackend(backend)}>
            {backend.enabled ? t("settings.webSearch.disableBackend") : t("settings.webSearch.enableBackend")}
          </Button>
          <Button disabled={busy || !backend.enabled || settings.web_search.default_backend_id === backend.id}
            onClick={() => void update({ default_backend_id: backend.id })}>{t("settings.webSearch.makeDefault")}</Button>
          <Button icon={<Trash2 size={14} />} disabled={busy} onClick={() => void remove(backend.id)}>{t("actions.delete")}</Button>
        </div>
      </Card>)}
      {settings && settings.web_search.backends.length === 0 ? <p className="state-message">{t("settings.webSearch.empty")}</p> : null}
    </div>
    {error ? <p className="state-message state-message--error">{t(`settings.webSearch.errors.${error}`)}</p> : null}
    {error === "load" ? <Button disabled={busy} onClick={() => void load()}>{t("actions.retry")}</Button> : null}
  </div>;
}
