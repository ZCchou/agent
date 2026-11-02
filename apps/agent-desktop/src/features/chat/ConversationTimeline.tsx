import { memo, useMemo } from "react";
import type { ChangeSet, ChatMessage, MessageAttachment, ToolOperation, WebOperation } from "../../types/domain";
import type { ProjectDirectoryTarget, ProjectFileTarget } from "./markdown/markdownPolicy";
import { MessageTimelineItem } from "./MessageTimeline";
import { RunActivityGroup } from "./RunActivityGroup";
import { buildConversationTimeline } from "./timelineModel";

export const ConversationTimeline = memo(function ConversationTimeline({
  messages, changeSets, operations, webOperations, projectName, conversationTitle, onProjectFile, onProjectDirectory, onAttachment, onReferenceAttachment, referenceDisabled, onChangeSetResolved, onBeginEdit, onEdit, editDisabled,
}: {
  messages: ChatMessage[];
  changeSets: ChangeSet[];
  operations: ToolOperation[];
  webOperations: WebOperation[];
  projectName?: string;
  conversationTitle?: string;
  onProjectFile?: (target: ProjectFileTarget) => void;
  onProjectDirectory?: (target: ProjectDirectoryTarget) => void;
  onAttachment?: (attachment: MessageAttachment) => void;
  onReferenceAttachment?: (attachment: MessageAttachment) => void;
  referenceDisabled?: boolean;
  onBeginEdit?(): Promise<boolean>;
  onEdit?(message: ChatMessage, content: string): Promise<boolean | undefined>;
  editDisabled?: boolean;
  onChangeSetResolved(): void;
}) {
  const items = useMemo(() => buildConversationTimeline(messages, changeSets, operations, webOperations),
    [messages, changeSets, operations, webOperations]);
  return items.map((item) => item.kind === "message"
    ? <MessageTimelineItem key={item.key} message={item.message} onProjectFile={onProjectFile} onProjectDirectory={onProjectDirectory} onAttachment={onAttachment} onReferenceAttachment={onReferenceAttachment} referenceDisabled={referenceDisabled} onBeginEdit={onBeginEdit} onEdit={onEdit} editDisabled={editDisabled} />
    : <RunActivityGroup key={item.key} item={item} projectName={projectName}
      conversationTitle={conversationTitle} onResolved={onChangeSetResolved}
      onProjectFile={onProjectFile} onProjectDirectory={onProjectDirectory} />);
});
