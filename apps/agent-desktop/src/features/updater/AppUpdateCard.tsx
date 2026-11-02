import { useTranslation } from "react-i18next";
import { Card } from "../../components/ui/Card";
import { Button } from "../../components/ui/Button";
import { useApp } from "../../contexts/AppContext";
import { useAppUpdate } from "./AppUpdateProvider";
export function AppUpdateCard() {
  const { t } = useTranslation("common"); const update = useAppUpdate(); const { setCurrentPage } = useApp();
  const { view, working } = update;
  const busy = working || ["checking", "downloading", "installing"].includes(view.state);
  return <Card className="app-update-card">
    <h3>{t("settings.updater.title")}</h3>
    <p>{t("settings.updater.current", { version: view.current_version })}</p>
    <div className="app-update-controls">
      <label>{t("settings.updater.channel")}<select aria-label={t("settings.updater.channel")} value={update.channel} disabled={busy} onChange={(e) => update.setChannel(e.target.value as "preview" | "stable")}>
        <option value="preview">{t("settings.updater.preview")}</option><option value="stable">{t("settings.updater.stable")}</option>
      </select></label>
      <label><input type="checkbox" checked={update.autoCheck} onChange={(e) => update.setAutoCheck(e.target.checked)} />{t("settings.updater.auto")}</label>
    </div>
    <p role="status">{t(`settings.updater.states.${view.state}`, { version: view.version })}</p>
    {view.notes && <p className="app-update-notes">{view.notes}</p>}
    {view.state === "downloading" && <div><progress aria-label={t("settings.updater.progress")} value={view.total_bytes ? view.downloaded_bytes : undefined} max={view.total_bytes ?? undefined} /><span>{t("settings.updater.bytes", { count: view.downloaded_bytes })}{view.total_bytes ? ` / ${view.total_bytes}` : ""}</span></div>}
    {update.error && <p role="alert">{t(`settings.updater.errors.${update.error}`, { defaultValue: t("settings.updater.errors.unknown") })}</p>}
    <div className="app-update-actions">
      <Button disabled={busy} onClick={() => void update.check()}>{t("settings.updater.check")}</Button>
      {view.state === "available" && <Button disabled={busy} onClick={() => void update.download()}>{t("settings.updater.download")}</Button>}
      {view.state === "ready" && <Button variant="primary" disabled={busy} onClick={() => void update.install()}>{t("settings.updater.install")}</Button>}
      {update.error && view.state !== "ready" && <Button disabled={busy} onClick={() => void update.retry()}>{t("settings.updater.retry")}</Button>}
      <Button variant="ghost" onClick={() => setCurrentPage("chat")}>{t("settings.updater.returnChat")}</Button>
    </div>
    <p className="app-update-hint">{t("settings.updater.installHint")}</p>
  </Card>;
}
export function AppUpdateBanner() {
  const update = useAppUpdate(); const { currentPage, setCurrentPage } = useApp(); const { t } = useTranslation("common");
  return <>
    {currentPage !== "settings" && ["available", "downloading", "ready"].includes(update.view.state) && <div className="app-update-banner" role="status"><span>{t("settings.updater.found", { version: update.view.version })}</span><Button onClick={() => setCurrentPage("settings")}>{t("settings.updater.open")}</Button></div>}
    {update.restoreConflict && <div className="app-update-banner" role="status"><span>{t("settings.updater.restoreConflict")}</span><Button onClick={update.requestRestore}>{t("settings.updater.restore")}</Button></div>}
    {update.missingAttachments > 0 && <div className="app-update-banner" role="status">{t("settings.updater.missing")}</div>}
    {update.restorationError && <div className="app-update-banner" role="alert"><span>{t("settings.updater.restoreError")}</span>
      <Button disabled={update.restoring} onClick={() => void update.retryRestoration()}>{t("settings.updater.retryDraft")}</Button></div>}
  </>;
}
