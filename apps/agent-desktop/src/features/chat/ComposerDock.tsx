import { BriefcaseBusiness, GitBranch } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { ReactNode } from "react";
import type { AttachmentPreview, CompactionTargetRequest, ContextCompactionState, Conversation, MessageAttachment, Project, ProjectGrant, RejectedAttachment } from "../../types/domain";
import { ChatComposer } from "./ChatComposer";
import { WorkExecutionControls } from "./WorkExecutionControls";
import { WebSearchControl } from "../web-search/WebSearchControl";
import type { WebSearchController } from "../web-search/useWebSearchController";
import { CompactionRecoveryControl } from "./CompactionRecoveryControl";
import type { RoutingModelOption } from "./ConversationRoutingControl";

interface Props {
  value: string;
  disabled: boolean;
  sendDisabled?: boolean;
  generating?: boolean;
  stopping?: boolean;
  onStop?(): void;
  referenceCount?: number;
  referenceTray?: ReactNode;
  compaction: ContextCompactionState | null;
  onPauseCompaction(): void;
  onResumeCompaction(target?: CompactionTargetRequest): void;
  compactionModels?: RoutingModelOption[];
  conversation: Conversation | null;
  project: Project | undefined;
  grant: ProjectGrant | null;
  isProjectWork: boolean;
  derivedSourceId: string | null;
  onChange(value: string): void;
  onSend(): void;
  onOpenSource(id: string): void;
  onGrantChange(grant: ProjectGrant | null): void;
  onConversationChange(conversation: Conversation): void;
  attachments: MessageAttachment[];
  rejectedAttachments: RejectedAttachment[];
  attachmentPreviews: Record<string, AttachmentPreview>;
  attachmentBusy: boolean;
  attachmentError: string | null;
  onAttach(): void;
  onPasteImages(files: File[]): void;
  onRemoveAttachment(id: string): void;
  onRemoveRejectedAttachment(id: string): void;
  webSearch: WebSearchController;
  onOpenProjectSettings?(): void;
  inlineNotice?: ReactNode;
  routingControl?: ReactNode;
}

export function ComposerDock(props: Props) {
  const { t } = useTranslation("chat");
  return <div className="chat-composer-area">
    {props.referenceTray}
    <ChatComposer value={props.value} onChange={props.onChange} onSend={props.onSend} disabled={props.disabled}
      sendDisabled={props.sendDisabled}
      generating={props.generating} stopping={props.stopping} onStop={props.onStop}
      referenceCount={props.referenceCount}
      attachments={props.attachments} rejectedAttachments={props.rejectedAttachments}
      previews={props.attachmentPreviews} attachmentBusy={props.attachmentBusy}
      attachmentError={props.attachmentError} onAttach={props.onAttach} onPasteImages={props.onPasteImages}
      onRemoveAttachment={props.onRemoveAttachment} onRemoveRejectedAttachment={props.onRemoveRejectedAttachment}
      webSearchControl={<WebSearchControl controller={props.webSearch} disabled={props.disabled || props.generating}
        onOpenProjectSettings={props.onOpenProjectSettings} />} />
    {props.inlineNotice}
    {props.compaction && ["queued", "running", "interrupted", "failed"].includes(props.compaction.status) ? (
      <div className={`context-compaction-status context-compaction-status--${props.compaction.status}`} role="status">
        <span>{props.compaction.status === "running" || props.compaction.status === "queued"
          ? t("context.compacting")
          : t(`context.compactionErrors.${props.compaction.error_code ?? "context_compaction_failed"}`, {
            defaultValue: t("context.compactionPaused"),
          })}</span>
        {props.compaction.status === "running" || props.compaction.status === "queued"
          ? <button type="button" onClick={props.onPauseCompaction}>{t("context.pauseCompaction")}</button>
          : <CompactionRecoveryControl key={props.compaction.id} options={props.compactionModels ?? []}
            disabled={props.disabled} needsNewTarget={props.compaction.error_code === "context_compaction_unreliable"}
            onResume={props.onResumeCompaction} />}
      </div>
    ) : null}
    <div className="compact-work-context">
      {props.routingControl}
      {props.derivedSourceId ? <button type="button" onClick={() => props.onOpenSource(props.derivedSourceId as string)}>
        <GitBranch size={14} /><span>{t("derive.source")}</span>
      </button> : null}
      {props.isProjectWork ? props.conversation ? <WorkExecutionControls
        conversation={props.conversation}
        project={props.project}
        grant={props.grant}
        onGrantChange={props.onGrantChange}
        onConversationChange={props.onConversationChange}
      /> : <span className="compact-work-context__status"><BriefcaseBusiness size={14} />{t("tools.availableAfterFirstMessage")}</span>
        : <span className="compact-work-context__status">{t("context.chatOnly")}</span>}
    </div>
  </div>;
}
