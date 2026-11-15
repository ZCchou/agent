import { Bot, Database, ScanText } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { useApp } from "../../contexts/AppContext";
import { settingsRepository } from "../../services/runtime";
import type { AppSettings, RoutingPreferences } from "../../types/settings";
import { AuxiliaryConfiguration } from "../rag/AuxiliaryModels";
import { suspendedImportLibrary } from "../rag/ImportWizard";
import { LocalModelsPanel } from "./LocalModelsPanel";
import { RoutingDurationSettings } from "./RoutingDurationSettings";

export function ModelsPage() {
  const { t } = useTranslation('common');
  const { setCurrentPage } = useApp();
  const [tab, setTab] = useState<"generation" | "retrieval" | "ocr">("generation");
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [error, setError] = useState("");
  const refresh = useCallback(async () => {
    try { setSettings(await settingsRepository.getAppSettings()); setError(""); }
    catch (value) { setError(String(value)); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  async function save(next: Partial<RoutingPreferences>) {
    try { setSettings(await settingsRepository.updateRoutingPreferences(next)); setError(""); }
    catch (value) { setError(String(value)); }
  }
  return <div className="page-content models-page">
    <header className="page-heading models-page__heading"><h1>本地模型</h1></header>
    {suspendedImportLibrary() ? <Button onClick={() => setCurrentPage("rag")}>返回资料导入</Button> : null}
    {error ? <div role="alert"><p>{error}</p><Button onClick={() => void refresh()}>{t('actions.retry')}</Button></div> : null}
    <nav aria-label="模型配置" role="tablist" className="model-tabs">
      <Button role="tab" icon={<Bot />} aria-selected={tab === "generation"} onClick={() => setTab("generation")}>对话模型</Button>
      <Button role="tab" icon={<Database />} aria-selected={tab === "retrieval"} onClick={() => setTab("retrieval")}>检索模型</Button>
      <Button role="tab" icon={<ScanText />} aria-selected={tab === "ocr"} onClick={() => setTab("ocr")}>OCR 模型</Button>
    </nav>
    {tab === "generation" ? <>
      <LocalModelsPanel onChanged={refresh} onAuxiliary={() => setTab("retrieval")} />
      {settings?.routing ? <Card><h2>模型驻留</h2>
        <RoutingDurationSettings value={settings.routing} onChange={save} />
      </Card> : null}
    </> : <AuxiliaryConfiguration key={tab} kind={tab} />}
  </div>;
}
