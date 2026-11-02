import { ExternalLink, FileText, FolderOpen } from "lucide-react";
import { type ComponentPropsWithoutRef } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";
import { classifyLink, parseProjectDirectoryTarget, parseProjectFileTarget, type ProjectDirectoryTarget, type ProjectFileTarget } from "./markdownPolicy";

export function MarkdownLink({ href = "", children, onProjectFile, onProjectDirectory }: ComponentPropsWithoutRef<"a"> & {
  onProjectFile?: (target: ProjectFileTarget) => void;
  onProjectDirectory?: (target: ProjectDirectoryTarget) => void;
}) {
  const directoryTarget = parseProjectDirectoryTarget(href);
  if (directoryTarget && onProjectDirectory) {
    return <button type="button" className="markdown-link markdown-link--project" title={directoryTarget.path}
      onClick={() => onProjectDirectory(directoryTarget)}><span>{children}</span><FolderOpen size={12} aria-hidden="true" /></button>;
  }
  const projectTarget = parseProjectFileTarget(href);
  if (projectTarget && onProjectFile) {
    return <button type="button" className="markdown-link markdown-link--project" title={projectTarget.path}
      onClick={() => onProjectFile(projectTarget)}><span>{children}</span><FileText size={12} aria-hidden="true" /></button>;
  }
  if (classifyLink(href) !== "external") {
    return <span className="markdown-link--blocked">{children}</span>;
  }
  return (
    <button
      type="button"
      className="markdown-link"
      title={href}
      onClick={() => void openUrl(href).catch(() => undefined)}
    >
      <span>{children}</span><ExternalLink size={12} aria-hidden="true" />
    </button>
  );
}
