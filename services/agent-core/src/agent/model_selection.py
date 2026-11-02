from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from agent.providers import ProviderProfile
from agent.storage import CoreDataError, CoreRepository


@dataclass(frozen=True)
class ModelSelectionDecision:
    provider_profile_id: str
    model_id: str
    selection_source: str
    reason_code: str
    policy_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ModelSelector:
    def select(
        self,
        request: dict[str, Any],
        profiles: dict[str, ProviderProfile],
        repository: CoreRepository,
        conversation: dict[str, Any] | None,
    ) -> ModelSelectionDecision:
        raise NotImplementedError


class FixedModelSelector(ModelSelector):
    """Validates an explicit choice; future routers implement the same boundary."""

    def select(
        self,
        request: dict[str, Any],
        profiles: dict[str, ProviderProfile],
        repository: CoreRepository,
        conversation: dict[str, Any] | None,
    ) -> ModelSelectionDecision:
        selection = request.get("selection")
        if selection is None:
            selection = {
                "mode": "fixed",
                "provider_profile_id": request.get("provider_profile_id"),
                "model_id": request.get("model_id"),
                "source": request.get("selection_source", "global_default"),
            }
        if not isinstance(selection, dict):
            raise CoreDataError("invalid_model_selection", "Model selection must be an object.")
        if selection.get("mode", "fixed") != "fixed":
            raise CoreDataError("model_routing_not_enabled", "Automatic model routing is not enabled.")
        provider_id, model_id = selection.get("provider_profile_id"), selection.get("model_id")
        source = selection.get("source", "global_default")
        if not isinstance(provider_id, str) or not provider_id or not isinstance(model_id, str) or not model_id:
            raise CoreDataError("invalid_model_selection", "A provider profile and model are required.")
        if source not in {"global_default", "conversation_override", "user_once"}:
            raise CoreDataError("invalid_model_selection", "Model selection source is not supported.")
        profile = profiles.get(provider_id)
        if profile is None or not profile.enabled:
            raise CoreDataError("provider_disabled", "The selected provider is not enabled.")
        if model_id not in profile.allowed_model_ids:
            raise CoreDataError("model_not_enabled", "The selected model is not allowed.")
        capabilities = (profile.model_capabilities or {}).get(model_id, {})
        if capabilities.get("text") == "no":
            raise CoreDataError(
                "model_capability_unsupported", "The selected model cannot generate text.",
            )
        if conversation and conversation.get("kind") == "project_work":
            scope = repository.get_project_model_scope(conversation["project_id"])
            if scope["policy"] == "restricted" and not any(
                item["provider_profile_id"] == provider_id and item["model_id"] == model_id
                for item in scope["models"]
            ):
                raise CoreDataError("model_not_enabled", "The selected model is not allowed by this project.")
        return ModelSelectionDecision(provider_id, model_id, source, "fixed_selection")
