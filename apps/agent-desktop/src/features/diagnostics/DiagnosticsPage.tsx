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


export function DiagnosticsPage() {
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
    </div>
  );
}
