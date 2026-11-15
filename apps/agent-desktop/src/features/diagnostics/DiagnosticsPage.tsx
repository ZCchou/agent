import {
  Activity,
} from "lucide-react";

import {
  useTranslation,
} from "react-i18next";

import {
  Card,
} from "../../components/ui/Card";

import {
  StatusDot,
} from "../../components/ui/StatusDot";

import {
  useApp,
} from "../../contexts/AppContext";
import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Button } from "../../components/ui/Button";


export function DiagnosticsPage() {
  const [diagnostic, setDiagnostic] = useState("");
  const [busy, setBusy] = useState(false);
  async function exportTask() {
    setBusy(true);
    try { setDiagnostic(await invoke<string>("export_execution_diagnostics")); }
    catch (error) { setDiagnostic(String(error)); }
    finally { setBusy(false); }
  }
  const {
    t,
  } =
    useTranslation(
      "diagnostics",
    );


  const {
    coreOnline,
    coreStatus,
    refreshCoreStatus,
  } =
    useApp();


  return (
    <div className="diagnostics-shell">
      <div className="page-intro">
        <h2>
          {t("title")}
        </h2>

        <p>
          {t(
            "description",
          )}
        </p>
      </div>


      <Card className="diagnostics-core-card">
        <div className="diagnostics-core-icon">
          <Activity
            size={24}
          />
        </div>


        <div className="diagnostics-core-copy">
          <span className="diagnostics-label">
            {t(
              "core.label",
            )}
          </span>


          <div className="diagnostics-status">
            <StatusDot
              status={
                coreOnline
                  ? "online"
                  : "offline"
              }
            />

            <strong>
              {coreOnline
                ? t(
                    "core.online",
                  )
                : coreStatus.running
                  ? t("core.unresponsive")
                : t(
                    "core.offline",
                  )}
            </strong>
          </div>


          <span>
            {coreOnline
              ? t(
                  "core.ready",
                )
              : coreStatus.running
                ? t("core.unhealthy")
              : t(
                  "core.unavailable",
                )}
          </span>
        </div>


        <button
          className="diagnostics-refresh"
          onClick={() =>
            void refreshCoreStatus()
          }
        >
          {t(
            "core.refresh",
          )}
        </button>
      </Card>
      <Card><div className="execution-task-card__actions">
        <Button onClick={() => void invoke("open_log_directory").catch(error => setDiagnostic(String(error)))}>打开日志目录</Button>
        <Button busy={busy} disabled={!coreOnline} onClick={() => void exportTask()}>导出最近任务诊断</Button>
      </div>{diagnostic ? <p role="status" style={{ overflowWrap: "anywhere" }}>{diagnostic}</p> : null}</Card>
    </div>
  );
}
