import {
  Maximize2,
  Minus,
  X,
} from "lucide-react";

import {
  getCurrentWindow,
} from "@tauri-apps/api/window";

import agentLogo
  from "../../assets/agent-logo.svg";
import { useTranslation } from "react-i18next";
import { useState } from "react";

const appWindow =
  getCurrentWindow();


export function TitleBar() {
  const { t } = useTranslation("common");
  const [failed, setFailed] = useState(false);
  async function handleMinimize() {
    setFailed(false);
    try {
      await appWindow.minimize();
    } catch (error) {
      setFailed(true);
      console.error(
        "Failed to minimize window:",
        error,
      );
    }
  }


  async function handleToggleMaximize() {
    setFailed(false);
    try {
      await appWindow.toggleMaximize();
    } catch (error) {
      setFailed(true);
      console.error(
        "Failed to toggle maximize:",
        error,
      );
    }
  }


  async function handleClose() {
    setFailed(false);
    try {
      await appWindow.close();
    } catch (error) {
      setFailed(true);
      console.error(
        "Failed to close window:",
        error,
      );
    }
  }


  return (
    <header
      className="window-titlebar"
      data-tauri-drag-region
    >
      {/* =====================================================
          Left Side

          整块区域都属于拖拽区域。
          用户可以按住这里移动整个桌面窗口。
         ===================================================== */}

      <div
        className="window-titlebar__brand"
        data-tauri-drag-region
      >
        <div
          className="window-titlebar__logo"
          data-tauri-drag-region
        >
          <img
            src={agentLogo}
            alt=""
            draggable={false}
            data-tauri-drag-region
          />
        </div>

        <span
          data-tauri-drag-region
        >
          agent
        </span>
      </div>


      {/* =====================================================
          Window Controls

          按钮本身不能成为 drag region，
          否则点击时会触发窗口拖动。
         ===================================================== */}

      <div className="window-titlebar__controls">
        {failed ? <span className="window-control-error" role="alert">{t("buttonAudit.windowFailed")}</span> : null}
        <button
          type="button"
          className="window-control"
          aria-label={t("window.minimize")}
          title={t("window.minimize")}
          onClick={() =>
            void handleMinimize()
          }
        >
          <Minus size={16} />
        </button>


        <button
          type="button"
          className="window-control"
          aria-label={t("window.maximize")}
          title={t("window.maximize")}
          onClick={() =>
            void handleToggleMaximize()
          }
        >
          <Maximize2 size={14} />
        </button>


        <button
          type="button"
          className="window-control window-control--close"
          aria-label={t("window.close")}
          title={t("window.close")}
          onClick={() =>
            void handleClose()
          }
        >
          <X size={17} />
        </button>
      </div>
    </header>
  );
}
