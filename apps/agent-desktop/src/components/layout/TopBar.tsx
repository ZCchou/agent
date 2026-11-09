import { ProjectLibraries } from "../../features/rag/ProjectLibraries";
import { Box, BriefcaseBusiness, MessageCircle, UserRound } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { pageTitleKeys } from "../../app/navigation";
import { useApp } from "../../contexts/AppContext";
import { conversationRepository, localModelRepository, modelCatalogRepository, projectRepository, settingsRepository } from "../../services/runtime";
import { resolveSelectedModel, selectableModels } from "../../services/modelSelection";
import { PROJECT_CATEGORIES, type ProjectCategory, type ProjectModelScope } from "../../types/domain";
import type { AppSettings } from "../../types/settings";
import { useScopedAction } from "../../features/chat/useScopedAction";

export function TopBar() {
  const { t } = useTranslation("common");
  const {
    currentPage, coreOnline, activeConversation, projects, chatGroups, draft, setDraftCategory,
    refreshWorkspace, setCurrentPage,
  } = useApp();
  const project = projects.find((item) => item.id === activeConversation?.project_id);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [projectModelScope, setProjectModelScope] = useState<ProjectModelScope | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [scopeLoadError, setScopeLoadError] = useState(false);
  const [loadRevision, setLoadRevision] = useState(0);
  const hasManagedModels = settings?.models.providers.some(provider => provider.id.startsWith("managed:")) ?? false;
  const action = useScopedAction(activeConversation?.id ?? `draft:${draft.revision}`, t("buttonAudit.actionFailed"));
  const isProjectWork = (activeConversation?.kind ?? draft.kind) === "project_work";
  useEffect(() => {
    if (currentPage !== "chat" || !coreOnline || !hasManagedModels) return;
    const timer = window.setInterval(() => setLoadRevision(value => value + 1), 5_000);
    return () => window.clearInterval(timer);
  }, [currentPage, coreOnline, hasManagedModels]);
  useEffect(() => {
    let active = true;
    setLoadError(false);
    void settingsRepository.getAppSettings().then(value => { if (active) setSettings(value); })
      .catch(() => { if (active) setLoadError(true); });
    return () => { active = false; };
  }, [currentPage, coreOnline, loadRevision]);
  useEffect(() => {
    let active = true;
    const projectId = activeConversation ? activeConversation.project_id : draft.kind === "project_work" ? draft.projectId : null;
    setProjectModelScope(null);
    setScopeLoadError(false);
    if (!projectId) { setProjectModelScope(null); return; }
    void projectRepository.getModelScope(projectId).then(value => { if (active) setProjectModelScope(value); })
      .catch(() => { if (active) setScopeLoadError(true); });
    return () => { active = false; };
  }, [activeConversation?.id, activeConversation?.project_id, draft.kind, draft.projectId, currentPage, loadRevision]);
  const draftProject = projects.find((item) => item.id === draft.projectId);
  const category = activeConversation?.resolved_category
    ?? (draft.kind === "project_work" ? draftProject?.category ?? "other" : draft.category);

  async function updateStandardCategory(categoryValue: ProjectCategory) {
    if (!activeConversation || activeConversation.kind !== "standard_chat") return;
    await action.run(async () => {
      await conversationRepository.update(activeConversation.id, {
        title: activeConversation.title,
        category: categoryValue,
      });
      await refreshWorkspace();
    });
  }

  async function moveStandardChat(groupId: string | null) {
    if (!activeConversation || activeConversation.kind !== "standard_chat") return;
    await action.run(async () => { await conversationRepository.moveChat(activeConversation.id, groupId, 0); await refreshWorkspace(); });
  }

  async function updateConversationProvider(value: string) {
    if (!settings?.models) return;
    if (value === "__follow_default__" && activeConversation?.model_selection_mode === "automatic") {
      await action.run(async () => {
        await conversationRepository.updateRouting(activeConversation.id, "automatic", activeConversation.routing_preset, null, null);
        await refreshWorkspace();
      });
      return;
    }
    const [providerProfileId, modelId] = value.split("::", 2);
    if (!providerProfileId || !modelId) return;
    const candidate = modelCandidates.find(item => item.value === value);
    if (!candidate) return;
    await action.run(async (isCurrent) => {
      if (candidate.provider.id.startsWith("managed:") && candidate.model.status === "unavailable") {
        let { job } = await localModelRepository.load(modelId);
        const deadline = Date.now() + 310_000;
        while (["queued", "running"].includes(job.state ?? job.status ?? "queued")) {
          if (!isCurrent()) return;
          if (Date.now() >= deadline) {
            await localModelRepository.cancel(job.id);
            throw new Error("Local model loading timed out");
          }
          await new Promise(resolve => window.setTimeout(resolve, 500));
          if (!isCurrent()) return;
          const { jobs } = await localModelRepository.jobs();
          const updated = jobs.find(item => item.id === job.id);
          if (!updated) throw new Error("Local model loading task is missing");
          job = updated;
        }
        if ((job.state ?? job.status) !== "completed") throw new Error("Local model loading failed");
      }
      if (!isCurrent()) return;
      if (!activeConversation) {
        const result = await modelCatalogRepository.updateDefault(providerProfileId, modelId);
        if (isCurrent()) setSettings(result.settings);
        return;
      }
      await conversationRepository.updateRouting(
        activeConversation.id,
        activeConversation.model_selection_mode === "automatic" ? "automatic" : "fixed",
        activeConversation.routing_preset, providerProfileId, modelId,
      );
      await refreshWorkspace();
      if (isCurrent()) setLoadRevision(value => value + 1);
    });
  }

  const allModelCandidates = settings ? selectableModels(
    settings,
    activeConversation?.kind === "project_work" ? project : !activeConversation && draft.kind === "project_work" ? draftProject : undefined,
    projectModelScope, true,
  ) : [];
  const automaticMode = activeConversation?.model_selection_mode === "automatic";
  const modelCandidates = automaticMode
    ? allModelCandidates.filter(({ provider, model }) =>
      provider.kind !== "fake" && model.allow_automatic_routing)
    : allModelCandidates;
  const requestedProviderId = automaticMode
    ? activeConversation?.coordinator_provider_profile_id
    : activeConversation?.provider_profile_id;
  const requestedModelId = automaticMode
    ? activeConversation?.coordinator_model_id
    : activeConversation?.model_id;
  const selectedModelValue = automaticMode && !requestedProviderId
    ? "__follow_default__"
    : resolveSelectedModel(
    modelCandidates, requestedProviderId, requestedModelId,
  )?.value ?? "";
  const defaultModelLabel = resolveSelectedModel(
    allModelCandidates,
    settings?.models?.default_provider_profile_id,
    settings?.models?.default_model_id,
  );
  const headingTitle = currentPage === "chat"
    ? activeConversation?.title ?? (draft.kind === "standard_chat" ? t("conversation.newChat") : t("conversation.newWork"))
    : t(pageTitleKeys[currentPage]);

  return (
    <header className="top-bar">
      <div className="top-bar__heading">
        <h1 className="top-bar__title" title={headingTitle}>
          {headingTitle}
        </h1>
        {currentPage === "chat" ? (
          <span className="conversation-breadcrumb">
            {isProjectWork
              ? <BriefcaseBusiness size={13} />
              : <MessageCircle size={13} />}
            {activeConversation ? project?.name ?? t("conversation.chatOnly") : (draft.kind === "project_work"
              ? draftProject?.name
              : t("conversation.chatOnly"))}
          </span>
        ) : null}
      </div>

      <div className="top-bar__actions">
        {currentPage === "chat" && (activeConversation?.project_id || (draft.kind === "project_work" && draft.projectId)) ? <ProjectLibraries key={activeConversation?.project_id || draft.projectId} projectId={(activeConversation?.project_id || draft.projectId)!} compact /> : null}
        {action.error || loadError || scopeLoadError ? <span className="top-bar__error" role="alert">{action.error ?? t("buttonAudit.actionFailed")}
          {loadError || scopeLoadError ? <button disabled={action.busy} onClick={() => setLoadRevision(value => value + 1)}>{t("actions.retry")}</button> : null}
        </span> : null}
        {currentPage === "chat" ? (
          <label className="top-select">
            <select
              value={category}
              disabled={isProjectWork || action.busy}
              onChange={(event) => {
                const next = event.target.value as ProjectCategory;
                if (activeConversation) void updateStandardCategory(next);
                else setDraftCategory(next);
              }}
            >
              {PROJECT_CATEGORIES.map((item) => (
                <option value={item} key={item}>{t(`projects.categories.${item}`)}</option>
              ))}
            </select>
          </label>
        ) : null}

        {currentPage === "chat" && activeConversation?.kind === "standard_chat" ? (
          <label className="top-select">
            <select
              value={activeConversation.chat_group_id ?? ""}
              disabled={action.busy}
              onChange={(event) => void moveStandardChat(event.target.value || null)}
            >
              <option value="">{t("chatGroups.ungrouped")}</option>
              {chatGroups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
            </select>
          </label>
        ) : null}

        <label className="top-select">
          <Box size={16} />
          <select value={activeConversation ? selectedModelValue : defaultModelLabel?.value ?? ""}
            aria-busy={action.busy} disabled={action.busy || !settings || loadError}
            onFocus={() => setLoadRevision(value => value + 1)}
            onChange={(event) => void updateConversationProvider(event.target.value)}>
            <option value="" disabled>{t("models.chooseCompatibleModel")}</option>
            {automaticMode ? (
              <option value="__follow_default__">
                {t("models.followDefault", {
                  model: defaultModelLabel
                    ? `${defaultModelLabel.provider.name} / ${defaultModelLabel.model.id}`
                    : t("models.chooseCompatibleModel"),
                })}
              </option>
            ) : null}
            {modelCandidates.map(({ provider, model, value }) => <option value={value} key={value}>
              {!activeConversation && defaultModelLabel?.value === value
                ? t("models.followDefault", { model: `${provider.name} / ${model.id}` })
                : `${provider.name} / ${model.id}`}{model.status === "unavailable" ? " · 未加载，选择后加载" : ""}
            </option>)}
          </select>
        </label>
        {currentPage === "chat" && allModelCandidates.length === 0 && <button onClick={() => setCurrentPage("models")}>{t("models.chooseDefault")}</button>}

        {currentPage === "chat" && allModelCandidates.some(item => item.model.status === "unavailable" && item.provider.id.startsWith("managed:")) && <button onClick={() => setCurrentPage("models")}>加载本地模型</button>}

        <button className="profile-button" aria-label={t("topbar.profile")} title={t("topbar.profile")}
          onClick={() => setCurrentPage("settings")}>
          <UserRound size={19} />
        </button>
      </div>
    </header>
  );
}
