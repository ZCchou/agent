import { Check, Copy, TriangleAlert } from "lucide-react";
import { memo, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { copyText } from "./copyText";
import { MarkdownRenderer } from "./MarkdownRenderer";
import type { ProjectDirectoryTarget, ProjectFileTarget } from "./markdownPolicy";

export type AssistantMessageState = "complete" | "streaming" | "draft";

export interface AssistantMessageBodyProps {
  content: string;
  state: AssistantMessageState;
  onProjectFile?: (target: ProjectFileTarget) => void;
  onProjectDirectory?: (target: ProjectDirectoryTarget) => void;
}

export const AssistantMessageBody = memo(function AssistantMessageBody({ content, state, onProjectFile, onProjectDirectory }: AssistantMessageBodyProps) {
  const { t } = useTranslation("chat");
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  useEffect(() => {
    setCopyState("idle");
  }, [content]);

  useEffect(() => {
    if (copyState === "idle") return;
    const timer = window.setTimeout(() => setCopyState("idle"), 1_800);
    return () => window.clearTimeout(timer);
  }, [copyState]);

  async function copyMarkdown() {
    try {
      await copyText(content);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  }

  return (
    <div className="chat-message__bubble chat-message__bubble--assistant">
      <MarkdownRenderer content={content} state={state} onProjectFile={onProjectFile} onProjectDirectory={onProjectDirectory} />
      {state !== "streaming" && content ? (
        <div className="assistant-message-actions">
          <button type="button" onClick={() => void copyMarkdown()}>
            {copyState === "copied" ? <Check size={13} /> : copyState === "failed" ? <TriangleAlert size={13} /> : <Copy size={13} />}
            {copyState === "copied"
              ? t("markdown.copied")
              : copyState === "failed" ? t("markdown.copyFailed") : t("markdown.copyMarkdown")}
          </button>
        </div>
      ) : null}
    </div>
  );
});
