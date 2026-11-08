import { Button } from "../../components/ui/Button";
import { useTranslation } from "react-i18next";
import { Card } from "../../components/ui/Card";
import { useApp } from "../../contexts/AppContext";
import { useAppUpdate } from "./AppUpdateProvider";
export function AppUpdateCard() {
  const { t } = useTranslation("common");
  const { setCurrentPage } = useApp();
  return <Card className="app-update-card"><h3>{t("settings.updater.title")}</h3><p>离线模式已停用在线更新。</p>
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
