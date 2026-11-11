import { Button, ButtonGroup } from "../../components/ui/Button";
import { Download, RefreshCw, RotateCcw } from 'lucide-react';
import { useTranslation } from "react-i18next";
import { Card } from "../../components/ui/Card";
import { useApp } from "../../contexts/AppContext";
import { useAppUpdate } from "./AppUpdateProvider";
export function AppUpdateCard() {
  const { t } = useTranslation("common");
  const { setCurrentPage } = useApp();
  const update = useAppUpdate();
  const { view } = update;
  return <Card className="app-update-card"><h3>{t("settings.updater.title")}</h3>
    <p>{t('settings.updater.current', { version: view.current_version })}</p>
    <p>{t('settings.updater.programHint')}</p>
    <p role="status">{t(`settings.updater.states.${view.state}`, { version: view.version })}</p>
    {view.notes && <pre className="app-update-notes">{view.notes}</pre>}
    {view.state === 'downloading' && <progress aria-label={t('settings.updater.progress')} value={view.total_bytes ? view.downloaded_bytes : undefined} max={view.total_bytes || undefined} />}
    {update.error && <p role="alert">{t(`settings.updater.errors.${update.error}`)}</p>}
    <ButtonGroup label={t('settings.updater.title')}>
      <Button icon={<RefreshCw />} busy={view.state === 'checking'} disabled={update.working} onClick={() => void update.check()}>{t('settings.updater.checkProgram')}</Button>
      {view.state === 'available' && <Button icon={<Download />} variant="primary" disabled={update.working} onClick={() => void update.download()}>{t('settings.updater.downloadProgram')}</Button>}
      {view.state === 'ready' && <Button icon={<RotateCcw />} variant="primary" disabled={update.working} onClick={() => void update.install()}>{t('settings.updater.install')}</Button>}
    </ButtonGroup>
    {view.state === 'ready' && <p>{t('settings.updater.installHint')}</p>}
    <Button variant="ghost" onClick={() => setCurrentPage("chat")}>{t("settings.updater.returnChat")}</Button>
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
