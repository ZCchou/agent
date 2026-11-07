import { lazy, Suspense } from "react";
import { Sidebar } from "../components/layout/Sidebar";
import { TopBar } from "../components/layout/TopBar";
import { PageContainer } from "../components/layout/PageContainer";
import { TitleBar } from "../components/layout/TitleBar";

import { useApp } from "../contexts/AppContext";

import { AppUpdateBanner } from "../features/updater/AppUpdateCard";
import { ChatPage } from "../features/chat/ChatPage";
import { useAppUpdate } from "../features/updater/AppUpdateProvider";
import { useTranslation } from "react-i18next";

const ProjectsPage = lazy(() => import("../features/projects/ProjectsPage").then((module) => ({ default: module.ProjectsPage })));
const RagPage = lazy(() => import("../features/rag/RagPage").then((module) => ({ default: module.RagPage })));
const ModelsPage = lazy(() => import("../features/models/ModelsPage").then((module) => ({ default: module.ModelsPage })));
const AbilitiesPage = lazy(() => import("../features/abilities/AbilitiesPage").then((module) => ({ default: module.AbilitiesPage })));
const MemoryPage = lazy(() => import("../features/memory/MemoryPage").then((module) => ({ default: module.MemoryPage })));
const RemindersPage = lazy(() => import("../features/reminders/RemindersPage").then((module) => ({ default: module.RemindersPage })));
const ApprovalsPage = lazy(() => import("../features/approvals/ApprovalsPage").then((module) => ({ default: module.ApprovalsPage })));
const SettingsPage = lazy(() => import("../features/settings/SettingsPage").then((module) => ({ default: module.SettingsPage })));
const DiagnosticsPage = lazy(() => import("../features/diagnostics/DiagnosticsPage").then((module) => ({ default: module.DiagnosticsPage })));


export function AppShell() {
  const { installing } = useAppUpdate();
  const { t } = useTranslation("common");
  const {
    currentPage,
  } = useApp();


  function renderPage() {
    switch (currentPage) {
      case "rag":
        return <RagPage />;
      case "projects":
        return <ProjectsPage />;

      case "models":
        return <ModelsPage />;

      case "abilities":
        return <AbilitiesPage />;

      case "memory":
        return <MemoryPage />;

      case "reminders":
        return <RemindersPage />;

      case "approvals":
        return <ApprovalsPage />;

      case "settings":
        return <SettingsPage />;

      case "diagnostics":
        return <DiagnosticsPage />;

      case "chat":
      default:
        return null;
    }
  }


  return (
    <div className="window-root">
      {/* 自定义桌面标题栏。
          Windows 原生 decorations 关闭以后，
          由这里提供拖动、最小化、最大化和关闭。 */}
      <TitleBar />


      {installing && <div className="app-update-installing" role="status" aria-live="assertive">{t("settings.updater.states.installing")}</div>}
      <div className="app-shell" inert={installing} aria-busy={installing}>
        <Sidebar />


        <section className="app-workspace">
          <TopBar />


          <div className="workspace-content">
          <AppUpdateBanner />
          <PageContainer>
            <div className="retained-chat" hidden={currentPage !== "chat"}><ChatPage /></div>
            <Suspense fallback={<div className="state-message" role="status" aria-busy="true" />}>
              {renderPage()}
            </Suspense>
          </PageContainer>
          </div>
        </section>
      </div>
    </div>
  );
}
