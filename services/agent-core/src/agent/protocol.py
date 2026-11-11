from __future__ import annotations

import logging
import os
import traceback
from typing import Any

from agent.infrastructure.logging import events, log_event
from agent.runtime import ChatRunService
from agent.repositories.context_repository import ContextRepository
from agent.storage import SCHEMA_VERSION, CoreDataError, CoreRepository


PROTOCOL_VERSION = 22
CORE_VERSION = "0.15.0"
BUILD_ID = "schema29-rag-workflow-v1"
_LOGGER = logging.getLogger("agent.core")


def _log_internal_error(error: Exception, request_id: str, message_type: str) -> None:
    # Keep semantic request data out of logs.  Traceback frames are useful for
    # diagnosis without recording prompts, provider bodies, queries, or keys.
    frames = traceback.extract_tb(error.__traceback__)
    log_event(
        _LOGGER, logging.ERROR, events.IPC_REQUEST_FAILED, "Core request failed",
        request_id=request_id, message_type=message_type,
        error_type=type(error).__name__,
        stack=[f"{frame.filename}:{frame.lineno}:{frame.name}" for frame in frames],
    )


def _error(
    request_id: str, message_type: str, code: str, message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"v": PROTOCOL_VERSION, "id": request_id, "type": message_type,
            "error": error}


def _success(request_id: str, message_type: str, result: Any) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "id": request_id, "type": message_type, "result": result}


def _required_string(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise CoreDataError("invalid_params", f"{key} must be a non-empty string.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise CoreDataError("invalid_text_encoding", "Text contains an invalid Unicode character.") from error
    return value


def _optional_string(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is not None and not isinstance(value, str):
        raise CoreDataError("invalid_params", f"{key} must be a string or null.")
    return value


def _string(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str):
        raise CoreDataError("invalid_params", f"{key} must be a string.")
    return value


def _string_list(params: dict[str, Any], key: str) -> list[str]:
    value = params.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CoreDataError("invalid_params", f"{key} must be an array of strings.")
    return value


def _integer(params: dict[str, Any], key: str) -> int:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoreDataError("invalid_params", f"{key} must be an integer.")
    return value


def handle_message(message: Any, repository: CoreRepository | None = None) -> dict[str, Any]:
    if not isinstance(message, dict):
        return _error("", "protocol.error", "invalid_request", "Request must be a JSON object.")
    request_id = message.get("id") if isinstance(message.get("id"), str) else ""
    message_type = message.get("type") if isinstance(message.get("type"), str) else "protocol.error"
    if message.get("v") != PROTOCOL_VERSION:
        return _error(request_id, message_type, "unsupported_protocol", "Protocol version is not supported.")
    if not request_id:
        return _error("", message_type, "invalid_request_id", "Request id is required.")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return _error(request_id, message_type, "invalid_params", "Request params must be an object.")

    try:
        if message_type == "ping":
            return _success(request_id, message_type, {
                "pid": os.getpid(), "core_version": CORE_VERSION,
                "protocol_version": PROTOCOL_VERSION, "schema_version": SCHEMA_VERSION,
                "build_id": os.environ.get("AGENT_BUILD_ID", BUILD_ID),
            })
        supported = {
            "skill.list", "skill.import", "skill.inspect", "skill.set_enabled", "skill.remove", "skill.save",
            "chat_group.list", "chat_group.create", "chat_group.rename", "chat_group.delete",
            "chat_group.reorder", "project.list", "project.create", "project.update", "project.reorder",
            "project.delete_impact",
            "conversation.list", "conversation.get", "conversation.create",
            "conversation.update", "conversation.rag.update", "conversation.derive_work", "conversation.reorder",
            "conversation.move_chat", "conversation.move_work", "conversation.copy_work",
            "conversation.delete", "project.delete",
            "trash.list", "trash.restore", "trash.delete_permanently", "trash.empty", "trash.purge_expired",
            "model.run.get", "model.run.find", "context.preview", "context.source.preview",
            "knowledge.list", "knowledge.propose",
            "knowledge.publish", "knowledge.update", "knowledge.archive", "knowledge.pin",
            "knowledge.reject",
            "conversation.model.update", "project.model_policy.update",
            "project.model_scope.get", "project.model_scope.update",
            "conversation.web_search.update", "project.web_access.update",
            "web.operation.list", "web.operation.list_conversation", "web.source.preview",
            "work.purpose.update",
            "attachment.get",
            "memory.global_instructions.get", "memory.global_instructions.update",
            "memory.global_instructions.reset",
        }
        if message_type not in supported:
            return _error(request_id, message_type, "unsupported_message_type", "Message type is not supported.")
        if repository is None:
            return _error(request_id, message_type, "core_not_ready", "Core data repository is unavailable.")

        if message_type.startswith("skill."):
            from agent.skills import SkillRegistry
            skills = SkillRegistry(repository.database_path.parent / "skills.json")
            if message_type == "skill.list":
                result = {"skills": skills.list()}
            elif message_type == "skill.import":
                result = {"skill": skills.add(_required_string(params, "path"))}
            elif message_type == "skill.inspect":
                result = {"skill": skills.inspect(_required_string(params, "name"))}
            elif message_type == "skill.set_enabled":
                skills.set_enabled(_required_string(params, "skill_id"), params.get("enabled"))
                result = {"skills": skills.list()}
            elif message_type == "skill.save":
                result = {"skill": skills.save_skill(
                    _required_string(params, "name"), _required_string(params, "description"),
                    _required_string(params, "content"), expected_revision=_optional_string(params, "expected_revision"),
                )}
            else:
                skills.remove(_required_string(params, "skill_id"))
                result = {"skills": skills.list()}
        elif message_type == "chat_group.list":
            result = {"chat_groups": repository.list_chat_groups()}
        elif message_type == "chat_group.create":
            result = {"chat_group": repository.create_chat_group(_required_string(params, "name"))}
        elif message_type == "chat_group.rename":
            result = {"chat_group": repository.rename_chat_group(
                _required_string(params, "chat_group_id"), _required_string(params, "name")
            )}
        elif message_type == "chat_group.delete":
            repository.delete_chat_group(_required_string(params, "chat_group_id"))
            result = {"deleted": True}
        elif message_type == "chat_group.reorder":
            result = {"chat_groups": repository.reorder_chat_groups(_string_list(params, "ordered_ids"))}
        elif message_type == "project.list":
            result = {"projects": repository.list_projects()}
        elif message_type == "project.create":
            result = {"project": repository.create_project(
                _required_string(params, "name"), _required_string(params, "category")
            )}
        elif message_type == "project.update":
            result = {"project": repository.update_project(
                _required_string(params, "project_id"),
                _required_string(params, "name"),
                _required_string(params, "category"),
            )}
        elif message_type == "project.delete":
            result = {"trash_item": repository.delete_project(_required_string(params, "project_id"))}
        elif message_type == "project.delete_impact":
            result = repository.project_delete_impact(_required_string(params, "project_id"))
        elif message_type == "project.reorder":
            result = {"projects": repository.reorder_projects(_string_list(params, "ordered_ids"))}
        elif message_type == "conversation.list":
            result = {"conversations": repository.list_conversations(
                _optional_string(params, "kind"),
                _optional_string(params, "chat_group_id"),
                _optional_string(params, "project_id"),
            )}
        elif message_type == "conversation.get":
            result = {"conversation": repository.get_conversation(
                _required_string(params, "conversation_id")
            )}
        elif message_type == "conversation.create":
            result = {"conversation": repository.create_conversation(
                _required_string(params, "kind"),
                _required_string(params, "title"),
                _optional_string(params, "category"),
                _optional_string(params, "chat_group_id"),
                _optional_string(params, "project_id"),
            )}
        elif message_type == "conversation.update":
            result = {"conversation": repository.update_conversation(
                _required_string(params, "conversation_id"),
                _required_string(params, "title"),
                _optional_string(params, "category"),
            )}
        elif message_type == "conversation.rag.update":
            result = {"conversation": repository.update_conversation_rag(
                _required_string(params, 'conversation_id'), _required_string(params, 'mode'))}
        elif message_type == "conversation.derive_work":
            result = {"conversation": repository.derive_work(
                _required_string(params, "source_conversation_id"),
                _required_string(params, "project_id"),
                _optional_string(params, "title"),
            )}
        elif message_type == "conversation.reorder":
            result = {"conversations": repository.reorder_conversations(
                _required_string(params, "kind"),
                _optional_string(params, "container_id"),
                _string_list(params, "ordered_ids"),
            )}
        elif message_type == "conversation.move_chat":
            result = {"conversation": repository.move_chat(
                _required_string(params, "conversation_id"),
                _optional_string(params, "chat_group_id"),
                _integer(params, "target_index"),
            )}
        elif message_type == "conversation.move_work":
            result = {"conversation": repository.move_work(
                _required_string(params, "conversation_id"),
                _required_string(params, "project_id"),
                _integer(params, "target_index"),
            )}
        elif message_type == "conversation.copy_work":
            result = {"conversation": repository.copy_work(
                _required_string(params, "source_conversation_id"),
                _required_string(params, "project_id"),
                _optional_string(params, "title"),
            )}
        elif message_type == "conversation.delete":
            result = {"trash_item": repository.delete_conversation(
                _required_string(params, "conversation_id")
            )}
        elif message_type == "trash.list":
            result = {"items": repository.list_trash()}
        elif message_type == "trash.restore":
            result = repository.restore_trash(
                _required_string(params, "entity_type"), _required_string(params, "entity_id")
            )
        elif message_type == "trash.delete_permanently":
            result = repository.permanently_delete_trash(
                _required_string(params, "entity_type"), _required_string(params, "entity_id")
            )
        elif message_type == "trash.empty":
            result = repository.empty_trash(False)
        elif message_type == "trash.purge_expired":
            result = repository.empty_trash(True)
        elif message_type == "model.run.get":
            run_id = _optional_string(params, "run_id")
            conversation_id = _optional_string(params, "conversation_id")
            if run_id:
                run = repository.get_model_run(run_id)
            elif conversation_id:
                run = repository.latest_model_run(conversation_id)
            else:
                raise CoreDataError("invalid_params", "run_id or conversation_id is required.")
            result = {"run": run}
        elif message_type == "model.run.find":
            result = {"run": repository.get_model_run_by_idempotency_key(
                _required_string(params, "idempotency_key")
            )}
        elif message_type == "context.preview":
            result = {"snapshot": ContextRepository(repository).preview(_required_string(params, "run_id"))}
        elif message_type == "context.source.preview":
            result = {"source": ContextRepository(repository).source_preview(
                _required_string(params, "run_id"), _required_string(params, "item_id")
            )}
        elif message_type == "knowledge.list":
            result = {"knowledge": repository.list_project_knowledge(
                _required_string(params, "project_id"), params.get("include_archived") is True
            )}
        elif message_type == "knowledge.propose":
            result = {"proposal": repository.propose_knowledge(
                _required_string(params, "conversation_id"), _required_string(params, "knowledge_type"),
                _required_string(params, "title"), _required_string(params, "content"),
                _optional_string(params, "source_message_id"),
            )}
        elif message_type == "knowledge.publish":
            result = {"knowledge": repository.publish_knowledge(
                _required_string(params, "proposal_id"), _optional_string(params, "title"),
                _optional_string(params, "content"),
            )}
        elif message_type == "knowledge.reject":
            repository.reject_knowledge_proposal(_required_string(params, "proposal_id"))
            result = {"rejected": True}
        elif message_type in {"knowledge.update", "knowledge.archive", "knowledge.pin"}:
            pinned = params.get("pinned") if message_type == "knowledge.pin" else None
            if pinned is not None and not isinstance(pinned, bool):
                raise CoreDataError("invalid_params", "pinned must be a boolean.")
            result = {"knowledge": repository.update_knowledge(
                _required_string(params, "knowledge_id"),
                title=_optional_string(params, "title") if message_type == "knowledge.update" else None,
                content=_optional_string(params, "content") if message_type == "knowledge.update" else None,
                pinned=pinned, archive=params.get("archived") is True if message_type == "knowledge.archive" else None,
            )}
        elif message_type == "conversation.model.update":
            result = {"conversation": repository.update_conversation_model(
                _required_string(params, "conversation_id"), _optional_string(params, "provider_profile_id"),
                _optional_string(params, "model_id"),
            )}
        elif message_type == "project.model_policy.update":
            result = {"project": repository.update_project_model_policy(
                _required_string(params, "project_id"), _required_string(params, "model_access_policy")
            )}
        elif message_type == "project.model_scope.get":
            result = {"scope": repository.get_project_model_scope(
                _required_string(params, "project_id")
            )}
        elif message_type == "project.model_scope.update":
            models = params.get("models", [])
            if not isinstance(models, list):
                raise CoreDataError("invalid_params", "models must be an array.")
            result = {"scope": repository.update_project_model_scope(
                _required_string(params, "project_id"), _required_string(params, "policy"), models
            )}
        elif message_type == "conversation.web_search.update":
            result = {"conversation": repository.update_conversation_web_search(
                _required_string(params, "conversation_id"), _required_string(params, "mode")
            )}
        elif message_type == "project.web_access.update":
            result = {"project": repository.update_project_web_access(
                _required_string(params, "project_id"), _required_string(params, "policy")
            )}
        elif message_type == "web.operation.list":
            result = {"operations": repository.list_web_operations(
                _required_string(params, "run_id")
            )}
        elif message_type == "web.operation.list_conversation":
            result = {"operations": repository.list_conversation_web_operations(
                _required_string(params, "conversation_id")
            )}
        elif message_type == "web.source.preview":
            result = {"source": repository.preview_web_source(
                _required_string(params, "run_id"), _required_string(params, "source_id")
            )}
        elif message_type == "work.purpose.update":
            result = {"conversation": repository.update_work_purpose(
                _required_string(params, "conversation_id"),
                _optional_string(params, "purpose_override"),
            )}
        elif message_type == "attachment.get":
            result = {"attachment": repository.get_attachment(
                _required_string(params, "attachment_id")
            )}
        elif message_type == "memory.global_instructions.get":
            result = {"global_instructions": repository.get_global_instructions()}
        elif message_type == "memory.global_instructions.update":
            content = _string(params, "content")
            enabled = params.get("enabled", True)
            if not isinstance(enabled, bool):
                raise CoreDataError("invalid_params", "enabled must be a boolean.")
            result = {"global_instructions": repository.update_global_instructions(
                content, enabled=enabled,
            )}
        elif message_type == "memory.global_instructions.reset":
            result = {"global_instructions": repository.reset_global_instructions()}
        elif message_type == "attachment.model_copy.update":
            result = {"attachment": repository.update_attachment_model_copy(
                _required_string(params, "attachment_id"),
                _required_string(params, "model_storage_key"),
                _required_string(params, "model_media_type"),
                _integer(params, "model_byte_size"),
                _required_string(params, "model_sha256"),
                _integer(params, "model_width"),
                _integer(params, "model_height"),
                _required_string(params, "normalization_version"),
            )}
        return _success(request_id, message_type, result)
    except CoreDataError as error:
        return _error(request_id, message_type, error.code, error.message, error.details)
    except Exception as error:
        _log_internal_error(error, request_id, message_type)
        return _error(request_id, message_type, "internal_error", "The Core request failed.")


ASYNC_MESSAGE_TYPES = {
    "rag.request",
    "local_model.request",
    "document.artifact.get",
    "document.template.import",
    "app.update.prepare", "app.update.abort",
    "runtime.capabilities.configure", "runtime.capabilities.resolve",
    "provider.configure", "provider.clear", "provider.status", "provider.list_models",
    "provider.native_search.probe", "routing.configure",
    "model.run.start", "model.run.cancel", "model.run.retry", "model.run.context.continue",
    "model.run.plan.get", "model.run.budget.resolve", "conversation.routing.update", "conversation.rag.update",
    "context.compaction.status", "context.compaction.resume", "context.compaction.pause",
    "model.run.tool.status", "model.run.tool.resolve", "model.run.tool.reject",
    "model.run.tool.review", "model.tool_capability.probe", "tool.operation.list",
    "tool.operation.list_conversation",
    "web.operation.prepare", "web.operation.status", "web.operation.complete",
    "web.operation.fail",
}


async def handle_async_message(message: Any, service: ChatRunService) -> dict[str, Any]:
    from agent.execution.app_update import is_admission
    admitted = (isinstance(message, dict) and message.get("v") == PROTOCOL_VERSION
                and isinstance(message.get("id"), str) and bool(message["id"])
                and isinstance(message.get("params", {}), dict)
                and isinstance(message.get("type"), str)
                and is_admission(message))
    if admitted:
        if service.update_prepared_id is not None:
            return _error(message["id"], message["type"], "app_update_installing",
                          "The application is preparing an update. Your request has not been started.")
        service.update_admissions += 1
    try:
        return await _dispatch_async_message(message, service)
    finally:
        if admitted:
            service.update_admissions -= 1


async def _dispatch_async_message(message: Any, service: ChatRunService) -> dict[str, Any]:
    if not isinstance(message, dict):
        return _error("", "protocol.error", "invalid_request", "Request must be a JSON object.")
    request_id = message.get("id") if isinstance(message.get("id"), str) else ""
    message_type = message.get("type") if isinstance(message.get("type"), str) else "protocol.error"
    if message.get("v") != PROTOCOL_VERSION:
        return _error(request_id, message_type, "unsupported_protocol", "Protocol version is not supported.")
    if not request_id:
        return _error("", message_type, "invalid_request_id", "Request id is required.")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return _error(request_id, message_type, "invalid_params", "Request params must be an object.")
    try:
        if message_type == "rag.request":
            result = await service.rag.handle(_required_string(params, "action"), params.get("params", {}))
        elif message_type == "local_model.request":
            result = await service.handle_local_model(_required_string(params, "action"), params.get("params", {}))
        elif message_type == 'document.artifact.get':
            result = service.document_artifact(_required_string(params, 'run_id'), _required_string(params, 'artifact_id'))
        elif message_type == 'document.template.import':
            result = service.import_project_template(_required_string(params, 'operation_id'), _required_string(params, 'storage_key'), _required_string(params, 'sha256'))
        elif getattr(service, "offline", False) and message_type in {"web.operation.prepare", "provider.native_search.probe"}:
            raise CoreDataError("offline_network_forbidden", "离线模式禁用公网检索。")
        elif message_type in {"app.update.prepare", "app.update.abort"}:
            from agent.execution.app_update import prepare, abort
            update_id = _required_string(params, "update_id")
            result = (prepare if message_type == "app.update.prepare" else abort)(service, update_id)
        elif message_type == "runtime.capabilities.configure":
            enabled = params.get("host_reporting")
            if type(enabled) is not bool:
                raise CoreDataError("invalid_params", "host_reporting must be a boolean.")
            if "tool_budget_limits" in params:
                service.tool_loop.configure_limits(params["tool_budget_limits"])
            service.runtime_capabilities.configure(host_reporting=enabled)
            result = {"configured": True}
        elif message_type == "runtime.capabilities.resolve":
            result = {"accepted": service.runtime_capabilities.resolve(
                _required_string(params, "run_id"), _required_string(params, "request_id"), params.get("report"),
            )}
        elif message_type == "provider.configure":
            result = {"provider": service.configure_provider(params)}
        elif message_type == "routing.configure":
            result = {"routing": service.configure_routing(params)}
        elif message_type == "provider.clear":
            service.clear_provider(_required_string(params, "provider_profile_id"))
            result = {"cleared": True}
        elif message_type == "provider.status":
            result = {"provider": service.profile_status(_required_string(params, "provider_profile_id"))}
        elif message_type == "provider.list_models":
            result = await service.list_models(_required_string(params, "provider_profile_id"))
        elif message_type == "provider.native_search.probe":
            result = await service.probe_native_web_search(
                _required_string(params, "provider_profile_id"),
                _required_string(params, "model_id"),
                params.get("route_id") if isinstance(params.get("route_id"), str) else None,
                [item for item in params.get("capability_signature", []) if isinstance(item, str)]
                if isinstance(params.get("capability_signature"), list) else None,
            )
        elif message_type == "model.run.start":
            result = await service.start(params)
        elif message_type == "model.run.plan.get":
            result = {"plan": service.get_execution_plan(_required_string(params, "plan_id"))}
        elif message_type == "model.run.budget.resolve":
            result = {"run": await service.resolve_routing_budget(
                _required_string(params, "run_id"),
                _required_string(params, "plan_hash"),
                params.get("approved") is True,
            )}
        elif message_type == "conversation.routing.update":
            result = {"conversation": service.update_conversation_routing(
                _required_string(params, "conversation_id"),
                _required_string(params, "mode"),
                params.get("preset") if isinstance(params.get("preset"), str) else None,
                params.get("provider_profile_id") if isinstance(params.get("provider_profile_id"), str) else None,
                params.get("model_id") if isinstance(params.get("model_id"), str) else None,
            )}
        elif message_type == 'conversation.rag.update':
            result = {'conversation': service.repository.update_conversation_rag(
                _required_string(params, 'conversation_id'), _required_string(params, 'mode'))}
        elif message_type == "model.run.cancel":
            result = {"run": await service.cancel(_required_string(params, "run_id"))}
        elif message_type == "model.run.retry":
            result = {"run": await service.retry(
                _required_string(params, "run_id"), params.get("context_strategy", "default"),
                _optional_string(params, "web_search_override"),
            )}
        elif message_type == "model.run.context.continue":
            result = {"run": await service.continue_context(_required_string(params, "run_id"))}
        elif message_type == "context.compaction.status":
            result = {"compaction": service.compaction_status(
                _required_string(params, "conversation_id")
            )}
        elif message_type == "context.compaction.resume":
            target = params.get("target")
            if target is not None:
                if not isinstance(target, dict):
                    raise CoreDataError("invalid_params", "target must be an object.")
                target = {key: _required_string(target, key)
                          for key in ("provider_profile_id", "model_id")}
            result = {"compaction": await service.resume_compaction(
                _required_string(params, "conversation_id"), target=target,
            )}
        elif message_type == "context.compaction.pause":
            result = {"compaction": await service.pause_compaction(
                _required_string(params, "conversation_id")
            )}
        elif message_type == "model.run.tool.status":
            operation = service.tool_status(
                _required_string(params, "operation_id"),
                _required_string(params, "status"),
            )
            await service.emit_tool_status(operation)
            result = {"operation": operation}
        elif message_type == "model.run.tool.resolve":
            success = params.get("success")
            if not isinstance(success, bool):
                raise CoreDataError("invalid_params", "success must be a boolean.")
            result = {"operation": service.resolve_tool(
                _required_string(params, "operation_id"),
                success,
                _string(params, "output"),
                _optional_string(params, "error_code"),
            )}
        elif message_type == "model.run.tool.reject":
            result = {"operation": service.resolve_tool(
                _required_string(params, "operation_id"),
                False,
                _string(params, "reason"),
                "tool_rejected_by_user",
                rejected=True,
            )}
        elif message_type == "tool.operation.list_conversation":
            result = {"operations": service.tool_loop.repository.list_for_conversation(
                _required_string(params, "conversation_id"))}
        elif message_type == "tool.operation.list":
            result = {"operations": service.list_tool_operations(
                _required_string(params, "run_id")
            )}
        elif message_type == "web.operation.prepare":
            sequence = params.get("sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int):
                raise CoreDataError("invalid_params", "sequence must be an integer.")
            result = {"operation": service.repository.create_web_operation(
                _required_string(params, "run_id"), sequence,
                _required_string(params, "idempotency_key"),
                _required_string(params, "route"), _optional_string(params, "backend_id"),
                _required_string(params, "query"), _required_string(params, "operation_id"),
            )}
        elif message_type == "web.operation.status":
            operation = service.repository.transition_web_operation(
                _required_string(params, "operation_id"), _required_string(params, "status")
            )
            if operation["status"] == "searching":
                await service.emit_web_operation_event(operation, "run.web_search_started")
            result = {"operation": operation}
        elif message_type == "web.operation.complete":
            sources, usage = params.get("sources"), params.get("usage", {})
            if not isinstance(sources, list) or not isinstance(usage, dict):
                raise CoreDataError("invalid_params", "sources and usage are invalid.")
            operation = service.repository.complete_web_operation(
                _required_string(params, "operation_id"), sources, usage
            )
            await service.emit_web_operation_event(operation, "run.web_search_completed")
            result = {"operation": operation}
        elif message_type == "web.operation.fail":
            operation = service.repository.transition_web_operation(
                _required_string(params, "operation_id"), "failed",
                error_code=_required_string(params, "error_code"),
            )
            await service.emit_web_operation_event(operation, "run.web_search_failed")
            result = {"operation": operation}
        elif message_type == "model.run.tool.review":
            result = await service.review_tool(params)
        elif message_type == "model.tool_capability.probe":
            result = await service.probe_tool_capability(
                _required_string(params, "provider_profile_id"),
                _required_string(params, "model_id"),
            )
        else:
            return _error(request_id, message_type, "unsupported_message_type", "Message type is not supported.")
        return _success(request_id, message_type, result)
    except CoreDataError as error:
        if message_type == "model.run.start" and isinstance(error.details, dict) \
                and error.details.get("phase") in {"requirements", "selection", "routing", "preflight"}:
            return _success(request_id, message_type, {
                "status": "preflight_rejected",
                "rejection": error.details,
            })
        return _error(request_id, message_type, error.code, error.message, error.details)
    except Exception as error:
        code = getattr(error, "code", "internal_error")
        message_text = getattr(error, "message", "The Core request failed.")
        if code == "internal_error":
            _log_internal_error(error, request_id, message_type)
        return _error(request_id, message_type, code, message_text)
