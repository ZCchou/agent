import { Globe2 } from "lucide-react";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { WebSearchMenu } from "./WebSearchMenu";
import type { WebSearchController as Controller } from "./useWebSearchController";

export function WebSearchControl({ controller, disabled, onOpenProjectSettings }: {
  controller: Controller;
  disabled?: boolean;
  onOpenProjectSettings?(): void;
}) {
  const { t } = useTranslation("chat");
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!controller.open) return;
    const close = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) controller.setOpen(false); };
    window.addEventListener("pointerdown", close);
    return () => window.removeEventListener("pointerdown", close);
  }, [controller]);
  const active = controller.mode === "auto" || controller.forceOnce
    || (controller.mode === "inherit" && controller.globalEnabled && controller.projectAllowed);
  return <div className="web-search-control" ref={root}>
    <button type="button" className={`chat-composer__icon web-search-control__trigger${active ? " is-active" : ""}`}
      aria-label={t("webSearch.title")} aria-expanded={controller.open} aria-pressed={active}
      disabled={disabled} onClick={() => controller.setOpen(!controller.open)}>
      <Globe2 size={19} />{controller.forceOnce ? <span aria-hidden="true" /> : null}
    </button>
    {controller.open ? <WebSearchMenu mode={controller.mode} forceOnce={controller.forceOnce}
      isProjectWork={controller.isProjectWork}
      globalEnabled={controller.globalEnabled} projectAllowed={controller.projectAllowed}
      hasSearchPath={controller.hasSearchPath} currentPath={controller.currentPath}
      busy={controller.busy} error={controller.error} onMode={(mode) => void controller.setMode(mode)}
      onForceOnce={() => { controller.forceSearchOnce(); controller.setOpen(false); }}
      onEnableGlobal={() => void controller.enableGlobal()}
      onAllowProject={() => void controller.allowProject()}
      onConnectTavily={controller.connectTavily}
      onRetrySettings={() => void controller.reloadSettings()}
      onOpenProjectSettings={onOpenProjectSettings ? () => {
        controller.setOpen(false);
        onOpenProjectSettings();
      } : undefined} /> : null}
  </div>;
}
