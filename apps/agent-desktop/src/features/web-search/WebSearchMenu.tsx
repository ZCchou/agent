import { Check, ExternalLink, Globe2, Search } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { webSearchMenuOptions } from "./webSearchPolicy";

interface Props {
  mode: "inherit" | "off" | "auto";
  forceOnce: boolean;
  isProjectWork: boolean;
  globalEnabled: boolean;
  projectAllowed: boolean;
  hasSearchPath: boolean;
  currentPath: { kind: "native" | "backend"; label: string } | null;
  busy: boolean;
  error: string | null;
  onMode(mode: "inherit" | "off" | "auto"): void;
  onForceOnce(): void;
  onEnableGlobal(): void;
  onAllowProject(): void;
  onConnectTavily(apiKey: string): Promise<void>;
  onOpenProjectSettings?(): void;
  onRetrySettings(): void;
}

export function WebSearchMenu(props: Props) {
  const { t } = useTranslation("chat");
  const [apiKey, setApiKey] = useState("");
  const [showTavily, setShowTavily] = useState(false);
  function connect() {
    if (!props.busy && apiKey.trim()) void props.onConnectTavily(apiKey.trim());
  }
  return <div className="web-search-menu" role="menu">
    {!props.globalEnabled ? <div className="web-search-menu__notice">
      <strong>{t("webSearch.privacyTitle")}</strong><p>{t("webSearch.privacyDescription")}</p>
      <button type="button" disabled={props.busy} onClick={props.onEnableGlobal}>{t("webSearch.enableGlobal")}</button>
    </div> : null}
    {props.globalEnabled && !props.projectAllowed ? <div className="web-search-menu__notice">
      <strong>{t("webSearch.projectDisabled")}</strong><p>{t("webSearch.projectDisabledHint")}</p>
      <button type="button" disabled={props.busy} onClick={props.onAllowProject}>
        {t("webSearch.allowProject")}
      </button>
      {props.onOpenProjectSettings ? <button type="button" onClick={props.onOpenProjectSettings}>
        <ExternalLink size={13} />{t("webSearch.openProjectSettings")}
      </button> : null}
    </div> : null}
    {props.globalEnabled && props.projectAllowed && !props.hasSearchPath ? <div className="web-search-menu__notice">
      <strong>{t("webSearch.noBackend")}</strong><p>{t("webSearch.noBackendHint")}</p>
      {!showTavily ? <button type="button" onClick={() => setShowTavily(true)}>
        {t("webSearch.connectTavily")}
      </button> : <div className="web-search-menu__setup">
        <label>{t("webSearch.tavilyKey")}<input type="password" autoComplete="off"
          disabled={props.busy} value={apiKey} onChange={(event) => setApiKey(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") { event.preventDefault(); event.stopPropagation(); connect(); }
          }} /></label>
        <button type="button" onClick={connect} disabled={props.busy || !apiKey.trim()}>{t("webSearch.connect")}</button>
      </div>}
    </div> : null}
    {webSearchMenuOptions(props.isProjectWork).includes("inherit") ? <button type="button" role="menuitemradio" aria-checked={props.mode === "inherit" && !props.forceOnce}
      disabled={props.busy} onClick={() => props.onMode("inherit")}><Globe2 size={16} /><span>{t("webSearch.inherit")}</span>{props.mode === "inherit" && !props.forceOnce ? <Check size={15} /> : null}</button> : null}
    <button type="button" role="menuitemradio" aria-checked={props.mode === "off" && !props.forceOnce}
      disabled={props.busy} onClick={() => props.onMode("off")}><Globe2 size={16} /><span>{t("webSearch.off")}</span>{props.mode === "off" && !props.forceOnce ? <Check size={15} /> : null}</button>
    <button type="button" role="menuitemradio" aria-checked={props.mode === "auto" && !props.forceOnce}
      disabled={props.busy || !props.globalEnabled || !props.projectAllowed}
      onClick={() => props.onMode("auto")}><Globe2 size={16} /><span>{t("webSearch.auto")}</span>{props.mode === "auto" && !props.forceOnce ? <Check size={15} /> : null}</button>
    <button type="button" role="menuitemradio" aria-checked={props.forceOnce}
      disabled={props.busy || !props.globalEnabled || !props.projectAllowed}
      title={t("webSearch.onceHint")}
      onClick={props.onForceOnce}><Search size={16} /><span>{t("webSearch.once")}</span>{props.forceOnce ? <Check size={15} /> : null}</button>
    <p className="web-search-menu__hint">{t("webSearch.onceHint")}</p>
    {props.currentPath ? <p className="web-search-menu__path">
      {t(props.currentPath.kind === "native" ? "webSearch.currentNativePath" : "webSearch.currentBackendPath", {
        name: props.currentPath.label,
      })}
    </p> : null}
    {props.error ? <p className="web-search-menu__error">{t("webSearch.saveFailed")}</p> : null}
    {props.error === "settings" ? <button type="button" onClick={props.onRetrySettings}>{t("common:actions.retry")}</button> : null}
  </div>;
}
