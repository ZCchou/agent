import { createContext, useContext, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Copy, FileText, FolderOpen } from "lucide-react";
import { ResourceContextMenu } from "../../resources/ResourceOverlays";

export const ProjectDirectoryContext = createContext<string | null>(null);

export function ProjectFileLink({ path, children, open, openDirectory }: {
  path: string; children: ReactNode; open(): void; openDirectory?: (target: { path: string }) => void;
}) {
  const root = useContext(ProjectDirectoryContext);
  const { t } = useTranslation("chat");
  const trigger = useRef<HTMLButtonElement>(null);
  const [point, setPoint] = useState<{ x: number; y: number } | null>(null);
  const [copyFailed, setCopyFailed] = useState(false);
  const win = root?.includes("\\");
  const displayRoot = root?.replace(/^\\\\\?\\UNC\\/i, "\\\\").replace(/^\\\\\?\\/, "").replace(/[\\/]$/, "");
  const fullPath = displayRoot !== undefined ? displayRoot + (win ? "\\" + path.split("/").join( "\\") : "/" + path) : path;
  const show = (x: number, y: number) => { setCopyFailed(false); setPoint({ x, y }); };
  return <>
    <button ref={trigger} type="button" className="markdown-link markdown-link--project" title={path} onClick={open}
      aria-haspopup={root ? "menu" : undefined}
      onContextMenu={event => { if (root) { event.preventDefault(); show(event.clientX, event.clientY); } }}
      onKeyDown={event => {
        if (root && (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10"))) {
          event.preventDefault(); const rect = event.currentTarget.getBoundingClientRect(); show(rect.left, rect.bottom);
        }
      }}><span>{children}</span><FileText size={12} aria-hidden="true" /></button>
    {point && root && <ResourceContextMenu label={fullPath} trigger={trigger.current} position={point}
      busy={false} onClose={() => setPoint(null)}>
      {openDirectory && <button type="button" role="menuitem" onClick={() => {
        setPoint(null); openDirectory({ path: path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "." });
      }}><FolderOpen size={15} />{t("fileMenu.location")}</button>}
      <button type="button" role="menuitem" onClick={async () => {
        try { await navigator.clipboard.writeText(fullPath); setPoint(null); } catch { setCopyFailed(true); }
      }}><Copy size={15} />{t("fileMenu.copy")}</button>
      {copyFailed && <input aria-label={t("fileMenu.copy")} readOnly value={fullPath} autoFocus onFocus={e => e.currentTarget.select()} />}
    </ResourceContextMenu>}
  </>;
}
