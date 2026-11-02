import type { Project, ProjectModelScope } from "../types/domain";
import type { AppSettings, ProviderProfileSettings, ProviderModelSettings } from "../types/settings";

export interface SelectableModel {
  provider: ProviderProfileSettings;
  model: ProviderModelSettings;
  value: string;
}

export function isLocalProvider(provider: ProviderProfileSettings): boolean {
  if (provider.kind === "fake" || provider.kind === "ollama") return true;
  try {
    const host = new URL(provider.resolved_api_base_url ?? provider.entered_base_url).hostname;
    return host === "localhost" || host === "127.0.0.1" || host === "::1";
  } catch {
    return false;
  }
}

export function selectableModels(
  settings: AppSettings,
  project?: Project,
  scope?: ProjectModelScope | null,
): SelectableModel[] {
  const applicableScope = scope && project?.id === scope.project_id ? scope : null;
  const scopeRestricted = project?.model_scope_policy === "restricted" || applicableScope?.policy === "restricted";
  return settings.models.providers.flatMap((provider) => {
    if (!provider.enabled) return [];
    if (project?.model_access_policy === "local_only" && !isLocalProvider(provider)) return [];
    return provider.models.filter((model) =>
      model.enabled
      && model.status !== "unavailable"
      && model.capabilities.text !== "no"
      && (!scopeRestricted || Boolean(applicableScope?.models.some((item) =>
        item.provider_profile_id === provider.id && item.model_id === model.id))),
    ).map((model) => ({ provider, model, value: `${provider.id}::${model.id}` }));
  });
}

export function resolveSelectedModel(
  candidates: SelectableModel[],
  providerId: string | null | undefined,
  modelId: string | null | undefined,
): SelectableModel | null {
  if (!providerId || !modelId) return null;
  return candidates.find((candidate) =>
    candidate.provider.id === providerId && candidate.model.id === modelId,
  ) ?? null;
}
