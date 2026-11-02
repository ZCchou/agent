import { Bot } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { ModelRunStatus } from "../../types/domain";
import { WorkProcessDisclosure } from "./WorkProcessDisclosure";
import type { ProjectDirectoryTarget, ProjectFileTarget } from "./markdown/markdownPolicy";

interface Props {
  status: ModelRunStatus;
  content: string;
  onProjectFile?: (target: ProjectFileTarget) => void;
  onProjectDirectory?: (target: ProjectDirectoryTarget) => void;
  activityLabel?: string | null;
}

export function assistantRunStatusKey(status: ModelRunStatus, hasContent: boolean) {
  if (status === "queued" || status === "building_context") return "preparing";
  if (status === "waiting_tool") return "workingFiles";
  return hasContent ? "generating" : "thinking";
}

export function AssistantRunMessage({ status, content, onProjectFile, onProjectDirectory, activityLabel }: Props) {
  const { t } = useTranslation("chat");
  const hasContent = content.length > 0;
  return <div className="chat-message chat-message--assistant chat-run-message">
    <span>{t("roles.agent")}</span>
    <div className="chat-run-message__status">
      <span className="chat-run-message__status-copy" role="status" aria-live="polite">
        <span className="chat-run-message__agent"><Bot size={14} /></span>
        <span>{activityLabel ?? t(`status.${assistantRunStatusKey(status, hasContent)}`)}</span>
        <span className="chat-run-message__dots" aria-hidden="true"><i /><i /><i /></span>
      </span>
    </div>
    {hasContent ? <div className="chat-run-message__content">
      <WorkProcessDisclosure content={content} state="streaming" onProjectFile={onProjectFile} onProjectDirectory={onProjectDirectory} />
    </div> : null}
  </div>;
}
