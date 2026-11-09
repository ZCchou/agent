import { useEffect, useRef, type ReactNode } from "react";
import { Button } from "../../components/ui/Button";
import type { RagJob } from "../../types/rag";
import "./rag.css";
import "./workspace.css";

export type LibraryTab = "documents" | "search" | "projects" | "settings";
export type RagView =
  | { kind: "home" }
  | { kind: "library"; id: string; tab: LibraryTab }
  | { kind: "document"; libraryId: string; versionId: string }
  | { kind: "tasks"; libraryId?: string };
export const phases: Record<string, string> = {
  queued: "等待处理",
  validating: "校验文件",
  parsing: "解析文档",
  ocr: "OCR 识别",
  validating_content: "检查解析结果",
  indexing: "建立检索索引",
  publishing: "发布检索资料",
  completed: "已完成",
  needs_attention: "需要处理",
  failed: "处理失败",
  interrupted: "已中断",
  cancelled: "已取消",
};
export const statuses: Record<string, string> = {
  queued: "排队中",
  running: "处理中",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
  cancelled: "已取消",
};
export const sources: Record<string, string> = {
  user_action: "用户界面操作",
  user_pause: "界面暂停请求",
  document_withdrawal: "文档撤回",
  library_removal: "资料库移除",
  app_exit: "应用退出",
  system_failure: "系统处理异常",
};
export const activeTask = (job: RagJob) =>
  ["queued", "running"].includes(job.status);
export const dateLabel = (value?: string | null) =>
  value ? new Date(value).toLocaleString() : "—";
export function navigateRag(view: RagView, navigate: (page: "rag") => void) {
  window.sessionStorage.setItem("agent:rag-view", JSON.stringify(view));
  window.dispatchEvent(new CustomEvent("agent:rag-navigate", { detail: view }));
  navigate("rag");
}
export function modelDestination(
  tab: "retrieval" | "ocr",
  navigate: (page: "models") => void,
) {
  window.sessionStorage.setItem("agent:model-tab", tab);
  navigate("models");
}
export function WorkspaceDialog({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLElement>(null);
  useEffect(() => {
    const previous =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    dialog.current?.focus();
    return () => previous?.focus();
  }, []);
  return (
    <div className="rag-modal-backdrop">
      <section
        ref={dialog}
        className="rag-modal workspace-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.stopPropagation();
            onClose();
          }
          if (event.key === "Tab") {
            const controls = Array.from(
              dialog.current?.querySelectorAll<HTMLElement>(
                "button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled)",
              ) ?? [],
            );
            const first = controls[0],
              last = controls[controls.length - 1];
            if (
              event.shiftKey &&
              (document.activeElement === first ||
                document.activeElement === dialog.current)
            ) {
              event.preventDefault();
              last?.focus();
            }
            if (!event.shiftKey && document.activeElement === last) {
              event.preventDefault();
              first?.focus();
            }
          }
        }}
      >
        <header className="rag-heading">
          <h2>{title}</h2>
          <Button onClick={onClose}>关闭</Button>
        </header>
        {children}
      </section>
    </div>
  );
}
