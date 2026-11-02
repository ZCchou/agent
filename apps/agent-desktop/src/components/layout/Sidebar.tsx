import { ChevronDown, ChevronRight, Plus } from "lucide-react";
import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { navigationItems } from "../../app/navigation";
import { useApp } from "../../contexts/AppContext";
import agentLogo from "../../assets/agent-logo.svg";
import { Button } from "../ui/Button";
import { StatusDot } from "../ui/StatusDot";
import { SidebarResourceTree } from "../../features/sidebar/SidebarResourceTree";
import { useResizableSidebar } from "../../features/sidebar/useResizableSidebar";

export function Sidebar() {
  const { t } = useTranslation("common");
  const {
    currentPage, setCurrentPage, coreOnline, coreStatus, startNewChat,
  } = useApp();

  const utilityItems = navigationItems.filter(
    (item) => item.availability !== "planned" && !["chat", "projects"].includes(item.id),
  );
  const [systemCollapsed, setSystemCollapsed] = useState(true);
  const showResizeSaveError = useCallback(() => {
    window.alert(t("sidebar.errors.saveWidth"));
  }, [t]);
  const resize = useResizableSidebar(showResizeSaveError);

  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <div className="sidebar-brand__icon">
          <img src={agentLogo} alt="agent" draggable={false} />
        </div>
        <div>
          <div className="sidebar-brand__name">agent</div>
          <div className="sidebar-brand__tagline">{t("brand.tagline")}</div>
        </div>
      </div>

      <Button variant="primary" className="sidebar-new-chat" icon={<Plus size={19} />} onClick={() => startNewChat()}>
        {t("navigation.newChat")}
      </Button>

      <SidebarResourceTree />

      <nav className="sidebar-navigation sidebar-navigation--compact">
        <button className="sidebar-system-toggle" onClick={() => setSystemCollapsed((value) => !value)} aria-expanded={!systemCollapsed}>
          {systemCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}{t("navigation.systemFeatures")}
        </button>
        {!systemCollapsed && utilityItems.map(({ id, labelKey, icon: Icon }) => (
          <button
            key={id}
            className={currentPage === id ? "sidebar-nav-item sidebar-nav-item--active" : "sidebar-nav-item"}
            onClick={() => setCurrentPage(id)}
          >
            <Icon size={17} /><span>{t(labelKey)}</span>
          </button>
        ))}
      </nav>

      <button className="core-status-card" onClick={() => setCurrentPage("diagnostics")}>
        <StatusDot status={coreOnline ? "online" : "offline"} />
        <div>
          <strong>{coreOnline ? t("core.online") : t("core.offline")}</strong>
          <span>{coreOnline ? t("core.operational") : coreStatus.issue?.includes("incompatible")
            ? t("core.rebuildSidecar") : t("core.openDiagnostics")}</span>
        </div>
      </button>

      <div
        className="sidebar-resize-handle"
        role="separator"
        aria-label={t("sidebar.resize")}
        aria-orientation="vertical"
        aria-valuemin={220}
        aria-valuemax={480}
        aria-valuenow={resize.width}
        tabIndex={0}
        onPointerDown={resize.onPointerDown}
        onKeyDown={resize.onKeyDown}
        onDoubleClick={resize.reset}
        title={t("sidebar.resizeHint")}
      />
    </aside>
  );
}
