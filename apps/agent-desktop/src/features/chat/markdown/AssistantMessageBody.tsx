import { Check, Copy, TriangleAlert } from "lucide-react";
import { memo, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { presentUnexecutedTools } from "./presentUnexecutedTools";
import { copyText } from "./copyText";
import { MarkdownRenderer } from "./MarkdownRenderer";
import type { ProjectDirectoryTarget, ProjectFileTarget } from "./markdownPolicy";

export type AssistantMessageState = "complete" | "streaming" | "draft";

export interface AssistantMessageBodyProps {
  content: string;
  state: AssistantMessageState;
  verifiedEvidenceIds?: readonly string[];
  documentReview?: boolean;
  targetLabels?: Readonly<Record<string, string>>;
  onProjectFile?: (target: ProjectFileTarget) => void;
  onProjectDirectory?: (target: ProjectDirectoryTarget) => void;
}

export const AssistantMessageBody = memo(function AssistantMessageBody({ content, state, onProjectFile, onProjectDirectory, verifiedEvidenceIds, documentReview, targetLabels }: AssistantMessageBodyProps) {
  const { t } = useTranslation("chat");
  const displayContent = presentUnexecutedTools(content, t("status.unexecutedTools"));
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
      await copyText(displayContent);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  }

  return (
    <div className="chat-message__bubble chat-message__bubble--assistant">
      <MarkdownRenderer verifiedEvidenceIds={verifiedEvidenceIds} content={displayContent} state={state} documentReview={documentReview}
        targetLabels={targetLabels} onProjectFile={onProjectFile} onProjectDirectory={onProjectDirectory} />
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
