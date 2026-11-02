import { FolderKey, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";
import { useApp } from "../../contexts/AppContext";
import { useScopedAction } from "./useScopedAction";
import {
  conversationRepository, settingsRepository, workPermissionRepository, workToolRepository,
} from "../../services/runtime";
import { PROJECT_CATEGORIES, type ApprovalMode, type Conversation, type Project, type ProjectGrant } from "../../types/domain";

interface Props {
  conversation: Conversation;
  project: Project | undefined;
  grant: ProjectGrant | null;
  onGrantChange(grant: ProjectGrant | null): void;
  onConversationChange(conversation: Conversation): void;
}

export function WorkExecutionControls({
  conversation, project, grant, onGrantChange, onConversationChange,
}: Props) {
  const { t } = useTranslation("chat");
  const { t: tCommon } = useTranslation("common");
  const { currentPage } = useApp();
  const [mode, setMode] = useState<ApprovalMode | null>(null);
  const [permissionError, setPermissionError] = useState(false);
  const [loadRevision, setLoadRevision] = useState(0);
  const [toolMode, setToolMode] = useState<"native" | "structured" | "text_only" | "unknown">("unknown");
  const action = useScopedAction(`${conversation.id}:${conversation.project_id}`, tCommon("buttonAudit.actionFailed"));
  const busy = action.busy;

  useEffect(() => {
    if (currentPage !== "chat") return;
    let active = true;
    setMode(null);
    setPermissionError(false);
    if (grant) {
      void workPermissionRepository.get(conversation.id).then((value) => {
        if (active) setMode(value.mode);
      }).catch(() => { if (active) setPermissionError(true); });
    }
    void settingsRepository.getAppSettings().then((settings) => {
      const automatic = conversation.model_selection_mode === "automatic";
      const providerId = automatic
        ? conversation.coordinator_provider_profile_id ?? settings.models.default_provider_profile_id
        : conversation.provider_profile_id ?? settings.models.default_provider_profile_id;
      const modelId = automatic
        ? conversation.coordinator_model_id ?? settings.models.default_model_id
        : conversation.model_id ?? settings.models.default_model_id;
      const model = settings.models.providers.find((item) => item.id === providerId)?.models.find((item) => item.id === modelId);
      if (active) setToolMode(model?.tool_mode ?? "unknown");
    }).catch(() => { if (active) setToolMode("unknown"); });
    return () => { active = false; };
  }, [conversation.id, conversation.model_id, conversation.provider_profile_id,
    conversation.model_selection_mode, conversation.coordinator_model_id,
    conversation.coordinator_provider_profile_id, grant, currentPage, loadRevision]);

  async function chooseDirectory() {
    if (!conversation.project_id) return;
    await action.run(async (isCurrent) => {
      const next = await workToolRepository.chooseProjectDirectory(conversation.project_id!);
      if (next && isCurrent()) onGrantChange(next);
    });
  }

  async function changeMode(next: ApprovalMode) {
    if (next === mode || !grant) return;
    await action.run(async (isCurrent) => {
      const updated = await workPermissionRepository.set(conversation.id, next);
      if (isCurrent()) setMode(updated.mode);
    });
  }

  async function changePurpose(value: string) {
    await action.run(async (isCurrent) => {
      const updated = await conversationRepository.updatePurpose(
        conversation.id,
        value ? value as Conversation["resolved_category"] : null,
      );
      if (isCurrent()) onConversationChange(updated);
    });
  }

  return (
    <section className="work-controls work-controls--compact" aria-label={t("tools.environmentTitle")}>
      <div className="work-controls__directory">
        <FolderKey size={16} />
        <span title={grant?.directory}>{grant ? t("tools.authorized", { path: grant.directory }) : t("tools.notAuthorized")}</span>
        <Button onClick={() => void chooseDirectory()} disabled={busy}>
          {grant ? t("tools.changeDirectory") : t("tools.authorizeDirectory")}
        </Button>
      </div>
      <label className="work-control-field">
        <ShieldCheck size={16} />
        <span>{t("permissions.label")}</span>
        <select value={mode ?? "open"} disabled={busy || !grant || mode === null} onChange={(event) => void changeMode(event.target.value as ApprovalMode)}>
          <option value="human">{t("permissions.human")}</option>
          <option value="ai_review">{t("permissions.aiReview")}</option>
          <option value="open">{t("permissions.open")}</option>
        </select>
      </label>
      {permissionError ? <p className="work-controls__warning" role="alert">{t("permissions.loadFailed")}
        <button disabled={busy} onClick={() => setLoadRevision(value => value + 1)}>{tCommon("actions.retry")}</button>
      </p> : null}
      {action.error ? <p className="work-controls__warning" role="alert">{action.error}</p> : null}
      <label className="work-control-field">
        <span>{t("purpose.label")}</span>
        <select value={conversation.purpose_override ?? ""} disabled={busy} onChange={(event) => void changePurpose(event.target.value)}>
          <option value="">{t("purpose.followProject", { purpose: t(`purpose.options.${project?.category ?? conversation.resolved_category}`) })}</option>
          {PROJECT_CATEGORIES.map((purpose) => <option value={purpose} key={purpose}>{t(`purpose.options.${purpose}`)}</option>)}
        </select>
      </label>
      {toolMode === "text_only" ? <p className="work-controls__warning">{t("tools.textOnlyModel")}</p> : null}
    </section>
  );
}
