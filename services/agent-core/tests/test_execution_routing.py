from __future__ import annotations

import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from agent.execution.domain import (
    CapabilityAdvisory, CapabilityRequirements, ExecutionBudget, InvocationTarget, ModelCandidate,
)
from agent.execution.coordinator import ExecutionCoordinator
from agent.execution.planner import ExecutionPlanner, PlanningError, PlanningRequest
from agent.execution.routes import InvocationRouteResolver
from agent.execution.compatibility import compatible_candidates
from agent.execution.selection import select_candidate
from agent.execution.requirements import explicit_requirements
from agent.execution.validator import PlanValidator
from agent.repositories.execution_repository import ExecutionRepository
from agent.runtime import ChatRunService
from agent.providers import ProviderProfile
from agent.storage import CoreRepository


def candidate(
    provider: str, model: str, *, vision: bool = False, tools: bool = True,
    local: bool = True, quality: int = 50, priced: bool = True,
    native_search: bool = False,
) -> ModelCandidate:
    return ModelCandidate(
        provider_profile_id=provider, model_id=model, protocol_id="openai_chat",
        location="local" if local else "online",
        capabilities={
            "text": "yes", "vision": "yes" if vision else "no",
            "vision_status": "verified" if vision else "unsupported",
            "reasoning": "yes", "context_length": 32768,
        },
        tool_mode="native" if tools else "text_only", quality_score=quality,
        native_search_status="verified" if native_search else "not_applicable",
        estimated_input_cost_per_million=Decimal("1") if priced else None,
        estimated_output_cost_per_million=Decimal("2") if priced else None,
    )


def request(**overrides):
    values = dict(
        selection_mode="automatic", routing_preset="balanced",
        requirements=CapabilityRequirements(),
        candidates=(candidate("p", "general"),), budget=ExecutionBudget(),
    )
    values.update(overrides)
    return PlanningRequest(**values)


def test_same_inputs_produce_stable_plan_hash() -> None:
    planner = ExecutionPlanner()
    first = planner.plan(request())
    second = planner.plan(request())
    assert first.plan_id != second.plan_id
    assert first.plan_hash == second.plan_hash


def test_plan_serializes_capability_advisories_without_changing_plain_plans() -> None:
    planner = ExecutionPlanner()
    plain = planner.plan(request())
    advisory = CapabilityAdvisory(
        capability_id="structured_web_evidence",
        requested_level="required",
        status="unavailable",
        reason_code="structured_search_route_unavailable",
        fallback_mode="general_knowledge_without_web",
        user_message_key="capability.structured_web_evidence.unavailable",
    )
    degraded = replace(plain, degraded_capabilities=(advisory,), plan_hash="")

    assert plain.to_dict()["degraded_capabilities"] == []
    assert degraded.to_dict()["degraded_capabilities"] == [advisory.to_dict()]
    assert degraded.plan_hash != plain.plan_hash


def test_degraded_requirements_keep_text_and_report_structured_search_loss() -> None:
    requirements = CapabilityRequirements(web="required", web_evidence="required")

    effective, advisories = ChatRunService._degraded_requirements(
        requirements, "structured_search_route_unavailable",
    ) or (None, ())

    assert effective is not None
    assert effective.text is True
    assert effective.web == "off"
    assert effective.web_evidence == "optional"
    assert [item.capability_id for item in advisories] == [
        "web_search", "structured_web_evidence",
    ]
    assert advisories[1].fallback_mode == "answer_without_verifiable_sources"


def test_degraded_requirements_never_relax_text_only_requests() -> None:
    assert ChatRunService._degraded_requirements(
        CapabilityRequirements(text=False), "no_compatible_execution_plan",
    ) is None


def test_capability_advisories_render_as_bounded_model_context() -> None:
    rendered = ChatRunService._render_capability_advisories([{
        "capability_id": "structured_web_evidence",
        "status": "unavailable",
        "reason_code": "structured_search_route_unavailable",
        "fallback_mode": "answer_without_verifiable_sources",
    }])
    assert rendered.startswith("<capability-advisories>")
    assert "structured_web_evidence" in rendered
    assert "answer_without_verifiable_sources" in rendered
    assert rendered.endswith("</capability-advisories>")


def test_degraded_file_capability_replaces_false_change_set_claims() -> None:
    content = "已提交删除变更集，等待批准中。"
    advisories = [{
        "capability_id": "file_write",
        "status": "unavailable",
        "reason_code": "model_tool_unsupported",
        "fallback_mode": "draft_without_file_access",
    }]

    guarded = ChatRunService._guard_degraded_file_response(content, advisories)

    assert "没有执行任何项目文件操作" in guarded
    assert "没有提交变更集" in guarded
    assert "等待批准" not in guarded


def test_execution_plan_persistence_defaults_advisories_for_legacy_rows(tmp_path) -> None:
    database = tmp_path / "plans.db"
    repository = CoreRepository(database)
    conversation = repository.create_conversation("standard_chat", "Chat", "casual")
    run = repository.start_model_run(
        content="hello", idempotency_key="legacy-plan", provider_profile_id="p",
        model_id="m", conversation_id=conversation["id"], kind="standard_chat",
        category="casual",
    )
    plan = ExecutionPlanner().plan(request())
    execution_repository = ExecutionRepository(database)
    execution_repository.save_plan(run["id"], plan, request().requirements.to_dict())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE execution_plans SET requirements_json = ? WHERE id = ?",
            ('{"text": true}', plan.plan_id),
        )
        connection.commit()
    restored = execution_repository.get_for_run(run["id"])
    assert restored is not None
    assert restored["requirements"].get("capability_advisories", []) == []


def test_degraded_route_remains_usable_but_healthy_route_is_preferred() -> None:
    base = candidate("p", "vision", vision=True)
    ready_target = InvocationTarget(
        "p", "vision", "ready", "vision", "openai_responses", "openai.responses",
        ("text", "vision"), "v1", "ready-fp", capability_status="declared",
    )
    degraded_target = replace(
        ready_target, route_id="degraded", route_config_fingerprint="degraded-fp",
        health_status="degraded", consecutive_failures=2,
    )
    ready = replace(base, invocation_target=ready_target, protocol_id="openai_responses")
    degraded = replace(base, invocation_target=degraded_target, protocol_id="openai_responses")
    requirements = CapabilityRequirements(vision="required")
    accepted, _ = compatible_candidates([degraded], requirements, automatic=True)
    assert accepted == [degraded]
    assert select_candidate([degraded, ready], preset="balanced") == ready


def test_active_cooling_and_configuration_blocks_are_not_reported_as_unsupported() -> None:
    base = candidate("p", "vision", vision=True)
    target = InvocationTarget(
        "p", "vision", "r", "vision", "openai_responses", "openai.responses",
        ("text", "vision"), "v1", "fp", capability_status="declared",
        health_status="cooling_down", health_retry_at="2999-01-01T00:00:00+00:00",
    )
    cooling = replace(base, invocation_target=target, protocol_id="openai_responses")
    accepted, rejected = compatible_candidates(
        [cooling], CapabilityRequirements(vision="required"), automatic=True,
    )
    assert accepted == []
    assert set(rejected.values()) == {"route_temporarily_cooling_down"}
    blocked = replace(cooling, invocation_target=replace(target, health_status="configuration_blocked"))
    _, rejected = compatible_candidates(
        [blocked], CapabilityRequirements(vision="required"), automatic=True,
    )
    assert set(rejected.values()) == {"route_configuration_blocked"}


def test_bound_text_coordinator_uses_one_vision_specialist() -> None:
    planner = ExecutionPlanner()
    plan = planner.plan(request(
        requirements=CapabilityRequirements(vision="required"),
        candidates=(candidate("p", "general"), candidate("v", "vision", vision=True)),
        coordinator_key=("p", "general"),
    ))
    assert [step.step_type for step in plan.steps] == ["vision_analysis", "final_response"]
    assert plan.coordinator_model_id == "general"
    assert plan.steps[0].model_id == "vision"


def test_first_image_request_prefers_single_capable_coordinator() -> None:
    plan = ExecutionPlanner().plan(request(
        requirements=CapabilityRequirements(vision="required"),
        candidates=(candidate("p", "general"), candidate("v", "vision", vision=True)),
    ))
    assert [step.step_type for step in plan.steps] == ["final_response"]
    assert plan.coordinator_model_id == "vision"


def test_fixed_mode_never_adds_specialist() -> None:
    with pytest.raises(PlanningError, match="no_compatible_execution_plan"):
        ExecutionPlanner().plan(request(
            selection_mode="fixed", fixed_key=("p", "general"),
            requirements=CapabilityRequirements(vision="required"),
            candidates=(candidate("p", "general"), candidate("v", "vision", vision=True)),
        ))


def test_unknown_online_specialist_price_requires_confirmation() -> None:
    plan = ExecutionPlanner().plan(request(
        requirements=CapabilityRequirements(vision="required"),
        candidates=(candidate("p", "general"),
                    candidate("v", "vision", vision=True, local=False, priced=False)),
        coordinator_key=("p", "general"),
    ))
    assert plan.requires_budget_confirmation is True
    assert plan.estimated_extra_online_cost_usd is None


def test_native_search_specialist_is_planned_without_replacing_coordinator() -> None:
    plan = ExecutionPlanner().plan(request(
        requirements=CapabilityRequirements(web="required"),
        candidates=(candidate("p", "general"),
                    candidate("s", "search", native_search=True, local=False)),
        coordinator_key=("p", "general"), web_route="specialist",
    ))
    assert [step.step_type for step in plan.steps] == ["web_research", "final_response"]
    assert plan.coordinator_model_id == "general"
    assert plan.steps[0].model_id == "search"


def test_required_structured_search_never_falls_back_to_opaque_route() -> None:
    with pytest.raises(PlanningError, match="structured_search_route_unavailable"):
        ExecutionPlanner().plan(request(
            requirements=CapabilityRequirements(web="required", web_evidence="required"),
            candidates=(candidate("p", "general"),
                        candidate("k", "kimi", native_search=True)),
            coordinator_key=("p", "general"), web_route="specialist",
        ))


def test_optional_specialist_search_is_guarded_by_persisted_intent_step() -> None:
    plan = ExecutionPlanner().plan(request(
        requirements=CapabilityRequirements(web="optional"),
        candidates=(candidate("p", "general"),
                    candidate("s", "search", native_search=True, local=False)),
        coordinator_key=("p", "general"), web_route="specialist",
    ))
    assert [step.step_type for step in plan.steps] == [
        "intent_analysis", "web_research", "final_response",
    ]
    assert plan.steps[1].required is False


def test_routing_intent_parser_is_strict_and_fallback_is_conservative() -> None:
    parsed = ExecutionCoordinator.parse_routing_intent(
        '{"web":"helpful","reasoning":"standard",'
        '"task_kinds":["research","research"],"explanation_code":"freshness"}'
    )
    assert parsed["task_kinds"] == ["research"]
    wrapped = ExecutionCoordinator.parse_routing_intent(
        "判断结果如下：\n```json\n"
        '{"web":"required","web_evidence":"required","reasoning":"standard",'
        '"task_kinds":["research"],"explanation_code":"current_meme"}'
        "\n```"
    )
    assert wrapped["web"] == "required"
    assert ExecutionCoordinator.fallback_routing_intent(
        "搜索最新的计算机视觉论文", [],
    )["web"] == "helpful"
    assert ExecutionCoordinator.fallback_routing_intent(
        "把这段文字改得更简洁", [],
    )["web"] == "not_needed"


def test_project_question_does_not_turn_available_tools_or_auto_web_into_hard_requirements() -> None:
    requirements = explicit_requirements(
        content="五条悟是谁", attachments=[], conversation_kind="project_work",
        web_request_mode="auto", reasoning_enabled=False, minimum_context_tokens=4096,
    )
    assert requirements.vision == "none"
    assert requirements.web == "off"
    assert requirements.files == "available"
    assert requirements.tools == "available"


def test_explicit_freshness_and_file_work_become_independent_hard_requirements() -> None:
    requirements = explicit_requirements(
        content="搜索最新资料并保存到 README", attachments=[],
        conversation_kind="project_work", web_request_mode="auto",
        reasoning_enabled=False, minimum_context_tokens=4096,
    )
    assert requirements.web == "required"
    assert requirements.files == "write"
    assert requirements.tools == "required"


def test_project_file_intent_recognizes_directory_inventory_and_bulk_delete() -> None:
    inventory = explicit_requirements(
        content="当前项目里有哪些文件？", attachments=[], conversation_kind="project_work",
        web_request_mode="off", reasoning_enabled=False, minimum_context_tokens=4096,
    )
    destructive = explicit_requirements(
        content="清空当前项目全部文件", attachments=[], conversation_kind="project_work",
        web_request_mode="off", reasoning_enabled=False, minimum_context_tokens=4096,
    )

    assert inventory.files == "read"
    assert inventory.tools == "required"
    assert destructive.files == "write"
    assert destructive.tools == "required"


def test_project_file_intent_recognizes_empty_directory_deletion() -> None:
    requirements = explicit_requirements(
        content="删除空目录", attachments=[], conversation_kind="project_work",
        web_request_mode="off", reasoning_enabled=False, minimum_context_tokens=4096,
    )

    assert requirements.files == "write"
    assert requirements.tools == "required"


def test_disabled_web_mode_wins_over_explicit_search_and_preserves_vision() -> None:
    requirements = explicit_requirements(
        content="上网搜一下他是谁", attachments=[{"kind": "image"}],
        conversation_kind="standard_chat", web_request_mode="off",
        reasoning_enabled=False, minimum_context_tokens=4096,
    )
    disabled = explicit_requirements(
        content="不要联网，只分析这张图", attachments=[{"kind": "image"}],
        conversation_kind="standard_chat", web_request_mode="auto",
        reasoning_enabled=False, minimum_context_tokens=4096,
    )

    assert requirements.web == "off"
    assert requirements.vision == "required"
    assert disabled.web == "off"


def test_minimum_context_requirement_accounts_for_recent_history_and_images() -> None:
    base = ChatRunService._minimum_context_requirement(None, "hello", [])
    expanded = ChatRunService._minimum_context_requirement({"messages": [
        {"role": "user", "content": "older context " * 100},
        {"role": "system_event", "content": "ignored " * 1000},
    ]}, "hello", [{"kind": "image", "model_width": 1024, "model_height": 768}])
    assert base >= 2048
    assert expanded > base


def test_ambiguous_deep_request_plans_conditional_reasoning_step() -> None:
    general = candidate("p", "general")
    general = ModelCandidate(
        **{**general.__dict__, "capabilities": {**general.capabilities, "reasoning": "no"}}
    )
    plan = ExecutionPlanner().plan(request(
        candidates=(general, candidate("r", "reasoner")),
        coordinator_key=("p", "general"), intent_analysis_needed=True,
    ))
    assert [step.step_type for step in plan.steps] == [
        "intent_analysis", "reasoning_analysis", "final_response",
    ]
    assert plan.steps[1].required is False


def test_call_count_over_automatic_budget_waits_for_confirmation() -> None:
    plan = ExecutionPlanner().plan(request(
        requirements=CapabilityRequirements(vision="required"),
        candidates=(candidate("p", "general"), candidate("v", "vision", vision=True)),
        coordinator_key=("p", "general"),
        budget=ExecutionBudget(max_model_calls=1),
    ))
    assert len(plan.steps) == 2
    assert plan.requires_budget_confirmation is True


def test_economy_mode_never_auto_selects_unknown_price_online_model() -> None:
    with pytest.raises(PlanningError, match="no_compatible_execution_plan"):
        ExecutionPlanner().plan(request(
            routing_preset="economy",
            candidates=(candidate("p", "unknown-price", local=False, priced=False),),
        ))


def test_v14_schema_creates_new_conversations_in_automatic_mode(tmp_path) -> None:
    path = tmp_path / "schema13.db"
    repository = CoreRepository(path)
    conversation = repository.create_conversation("standard_chat", "Existing", "casual")
    with sqlite3.connect(path) as connection:
        mode, version = connection.execute(
            "SELECT model_selection_mode, (SELECT version FROM schema_info) FROM conversations WHERE id = ?",
            (conversation["id"],),
        ).fetchone()
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
    assert mode == "automatic"
    assert version == 31
    assert {"execution_plans", "execution_steps", "execution_artifacts"}.issubset(tables)


def test_fixed_model_can_select_an_official_native_search_route_without_switching_model() -> None:
    primary = candidate("deepseek", "deepseek-v4-pro", native_search=False, local=False)
    search = ModelCandidate(**{
        **primary.__dict__,
        "protocol_id": "anthropic_messages",
        "native_search_status": "declared",
        "invocation_target": InvocationTarget(
            provider_profile_id="deepseek", model_id="deepseek-v4-pro",
            route_id="deepseek.anthropic_web", route_role="native_search",
            protocol="anthropic_messages", adapter_id="anthropic_messages_web_search",
            capability_signature=("text", "native_web_search"),
            route_manifest_version="invocation-routes-v2",
            route_config_fingerprint="deepseek-web-v2", capability_status="declared",
        ),
    })
    plan = ExecutionPlanner().plan(request(
        selection_mode="fixed", fixed_key=("deepseek", "deepseek-v4-pro"),
        requirements=CapabilityRequirements(web="required"),
        candidates=(primary, search), web_route="native",
    ))
    assert plan.coordinator_model_id == "deepseek-v4-pro"
    assert plan.steps[-1].protocol_id == "anthropic_messages"
    assert plan.steps[-1].invocation_route_id == "deepseek.anthropic_web"


def test_required_evidence_preplans_search_and_grounding_fallback_steps() -> None:
    primary = ModelCandidate(**{
        **candidate("deepseek", "deepseek-v4-pro", local=False).__dict__,
        "invocation_target": InvocationTarget(
            provider_profile_id="deepseek", model_id="deepseek-v4-pro",
            route_id="deepseek.openai_chat", route_role="primary",
            protocol="openai_chat", adapter_id="deepseek.openai_chat",
            capability_signature=("text",), route_manifest_version="v4",
            route_config_fingerprint="primary-v4", source_provenance="none",
            capability_status="declared",
        ),
    })
    search = ModelCandidate(**{
        **candidate("deepseek", "deepseek-v4-pro", native_search=True, local=False).__dict__,
        "protocol_id": "anthropic_messages",
        "invocation_target": InvocationTarget(
            provider_profile_id="deepseek", model_id="deepseek-v4-pro",
            route_id="deepseek.anthropic_web", route_role="native_search",
            protocol="anthropic_messages", adapter_id="deepseek.anthropic_web",
            capability_signature=("text", "native_web_search"),
            route_manifest_version="v4", route_config_fingerprint="search-v4",
            source_provenance="structured", capability_status="declared",
        ),
    })
    planner = ExecutionPlanner()
    requirements = CapabilityRequirements(web="required", web_evidence="required")
    route = planner.resolve_web_route(
        requirements=requirements, candidates=(primary, search),
        coordinator_key=primary.key, selection_mode="fixed",
        host_backend_id=None, host_backend_kind=None, request_mode="force_once",
    )
    plan = planner.plan(request(
        selection_mode="fixed", fixed_key=primary.key,
        requirements=requirements, candidates=(primary, search),
        web_route=route.route,
    ))
    assert [step.step_type for step in plan.steps] == [
        "web_research", "final_response", "grounding_fallback",
    ]
    assert plan.steps[-1].activation_condition == "when_web_evidence_missing"


def test_opaque_native_search_is_allowed_as_last_resort_for_required_evidence() -> None:
    opaque = ModelCandidate(**{
        **candidate("kimi", "kimi-k3", native_search=True, local=False).__dict__,
        "invocation_target": InvocationTarget(
            provider_profile_id="kimi", model_id="kimi-k3",
            route_id="kimi.openai_chat", route_role="native_search",
            protocol="openai_chat", adapter_id="kimi.openai_chat",
            capability_signature=("text", "native_web_search"),
            route_manifest_version="v4", route_config_fingerprint="kimi-v4",
            source_provenance="opaque", capability_status="verified",
        ),
    })
    route = ExecutionPlanner().resolve_web_route(
        requirements=CapabilityRequirements(web="required", web_evidence="required"),
        candidates=(opaque,), coordinator_key=opaque.key, selection_mode="fixed",
        host_backend_id=None, host_backend_kind=None, request_mode="force_once",
    )
    assert route.reason_code == "same_model_opaque_search_fallback"


def test_same_model_image_and_search_freezes_two_exact_routes() -> None:
    primary = candidate("deepseek", "deepseek-flash", local=False)
    primary = ModelCandidate(**{
        **primary.__dict__,
        "invocation_target": InvocationTarget(
            provider_profile_id="deepseek", model_id="deepseek-flash",
            route_id="deepseek.openai_chat", route_role="primary",
            protocol="openai_chat", adapter_id="deepseek.openai_chat",
            capability_signature=("text", "function_tools", "reasoning"),
            route_manifest_version="unified-invocation-routes-v3",
            route_config_fingerprint="primary-v3", capability_status="declared",
        ),
    })
    vision = ModelCandidate(**{
        **candidate("deepseek", "deepseek-flash", vision=True, local=False).__dict__,
        "protocol_id": "openai_responses",
        "invocation_target": InvocationTarget(
            provider_profile_id="deepseek", model_id="deepseek-flash",
            route_id="deepseek.responses", route_role="vision",
            protocol="openai_responses", adapter_id="deepseek.responses",
            capability_signature=("text", "vision"),
            route_manifest_version="unified-invocation-routes-v3",
            route_config_fingerprint="vision-v3", capability_status="declared",
        ),
    })
    search = ModelCandidate(**{
        **candidate("deepseek", "deepseek-flash", native_search=True, local=False).__dict__,
        "protocol_id": "anthropic_messages",
        "invocation_target": InvocationTarget(
            provider_profile_id="deepseek", model_id="deepseek-flash",
            route_id="deepseek.anthropic_web", route_role="native_search",
            protocol="anthropic_messages", adapter_id="deepseek.anthropic_web",
            capability_signature=("text", "native_web_search"),
            route_manifest_version="unified-invocation-routes-v3",
            route_config_fingerprint="search-v3", capability_status="declared",
        ),
    })
    plan = ExecutionPlanner().plan(request(
        requirements=CapabilityRequirements(vision="required", web="required"),
        candidates=(primary, vision, search),
        coordinator_key=("deepseek", "deepseek-flash"), web_route="native",
    ))
    assert [(step.step_type, step.invocation_route_id) for step in plan.steps] == [
        ("vision_analysis", "deepseek.responses"),
        ("final_response", "deepseek.anthropic_web"),
    ]
    assert plan.coordinator_model_id == "deepseek-flash"


def test_image_and_search_specialists_choose_least_privileged_route_signature() -> None:
    coordinator = ModelCandidate(**{
        **candidate("deepseek", "deepseek-v4-pro", local=False).__dict__,
        "invocation_target": InvocationTarget(
            provider_profile_id="deepseek", model_id="deepseek-v4-pro",
            route_id="deepseek.openai_chat", route_role="primary",
            protocol="openai_chat", adapter_id="deepseek.openai_chat",
            capability_signature=("text",),
            route_manifest_version="unified-invocation-routes-v3",
            route_config_fingerprint="primary", capability_status="declared",
        ),
    })
    vision_base = candidate("kimi", "kimi-vision", vision=True, local=False)
    vision_narrow = ModelCandidate(**{
        **vision_base.__dict__,
        "invocation_target": InvocationTarget(
            provider_profile_id="kimi", model_id="kimi-vision",
            route_id="kimi.openai_chat", route_role="primary",
            protocol="openai_chat", adapter_id="kimi.openai_chat",
            capability_signature=("text", "vision"),
            route_manifest_version="unified-invocation-routes-v3",
            route_config_fingerprint="kimi", capability_status="verified",
        ),
    })
    vision_with_tools = ModelCandidate(**{
        **vision_base.__dict__,
        "invocation_target": InvocationTarget(
            provider_profile_id="kimi", model_id="kimi-vision",
            route_id="kimi.openai_chat", route_role="primary",
            protocol="openai_chat", adapter_id="kimi.openai_chat",
            capability_signature=("function_tools", "text", "vision"),
            route_manifest_version="unified-invocation-routes-v3",
            route_config_fingerprint="kimi", capability_status="verified",
        ),
    })
    search = ModelCandidate(**{
        **candidate(
            "deepseek", "deepseek-v4-pro", native_search=True, local=False,
        ).__dict__,
        "protocol_id": "anthropic_messages",
        "invocation_target": InvocationTarget(
            provider_profile_id="deepseek", model_id="deepseek-v4-pro",
            route_id="deepseek.anthropic_web", route_role="native_search",
            protocol="anthropic_messages", adapter_id="deepseek.anthropic_web",
            capability_signature=("native_web_search", "text"),
            route_manifest_version="unified-invocation-routes-v3",
            route_config_fingerprint="search", capability_status="verified",
        ),
    })
    requirements = CapabilityRequirements(vision="required", web="required")
    plan = ExecutionPlanner().plan(request(
        requirements=requirements,
        candidates=(coordinator, vision_with_tools, vision_narrow, search),
        coordinator_key=coordinator.key, web_route="specialist",
    ))

    vision_step = next(step for step in plan.steps if step.step_type == "vision_analysis")
    web_step = next(step for step in plan.steps if step.step_type == "web_research")
    assert vision_step.capability_signature == ("text", "vision")
    assert web_step.provider_profile_id == coordinator.provider_profile_id
    assert web_step.model_id == coordinator.model_id
    assert web_step.invocation_route_id == "deepseek.anthropic_web"
    PlanValidator().validate(plan, requirements)


def test_vision_capable_final_still_creates_vision_artifact_before_search_specialist() -> None:
    vision = ModelCandidate(**{
        **candidate("kimi", "kimi-k2.6", vision=True, local=False).__dict__,
        "invocation_target": InvocationTarget(
            provider_profile_id="kimi", model_id="kimi-k2.6",
            route_id="kimi.openai_chat", route_role="primary",
            protocol="openai_chat", adapter_id="kimi.openai_chat",
            capability_signature=("text", "vision"),
            route_manifest_version="unified-invocation-routes-v3",
            route_config_fingerprint="kimi-vision", capability_status="verified",
        ),
    })
    search = ModelCandidate(**{
        **candidate("deepseek", "deepseek-v4-pro", native_search=True, local=False).__dict__,
        "protocol_id": "anthropic_messages",
        "invocation_target": InvocationTarget(
            provider_profile_id="deepseek", model_id="deepseek-v4-pro",
            route_id="deepseek.anthropic_web", route_role="native_search",
            protocol="anthropic_messages", adapter_id="deepseek.anthropic_web",
            capability_signature=("native_web_search", "text"),
            route_manifest_version="unified-invocation-routes-v3",
            route_config_fingerprint="deepseek-search", capability_status="verified",
        ),
    })
    requirements = CapabilityRequirements(vision="required", web="required")
    plan = ExecutionPlanner().plan(request(
        requirements=requirements, candidates=(vision, search),
        coordinator_key=vision.key, web_route="specialist",
    ))

    assert [step.step_type for step in plan.steps] == [
        "vision_analysis", "web_research", "final_response",
    ]
    assert (
        plan.steps[0].provider_profile_id,
        plan.steps[0].model_id,
        plan.steps[0].invocation_route_id,
    ) == (
        plan.steps[-1].provider_profile_id,
        plan.steps[-1].model_id,
        plan.steps[-1].invocation_route_id,
    )
    PlanValidator().validate(plan, requirements)


def test_fixed_model_can_split_native_search_from_file_tool_final_route() -> None:
    primary = candidate("deepseek", "deepseek-v4-pro", local=False)
    primary = ModelCandidate(**{
        **primary.__dict__,
        "invocation_target": InvocationTarget(
            provider_profile_id="deepseek", model_id="deepseek-v4-pro",
            route_id="deepseek.openai_chat", route_role="primary",
            protocol="openai_chat", adapter_id="deepseek.openai_chat",
            capability_signature=("text", "function_tools", "reasoning"),
            route_manifest_version="unified-invocation-routes-v3",
            route_config_fingerprint="primary-v3", capability_status="declared",
        ),
    })
    search = ModelCandidate(**{
        **candidate(
            "deepseek", "deepseek-v4-pro", native_search=True, local=False,
        ).__dict__,
        "protocol_id": "anthropic_messages",
        "invocation_target": InvocationTarget(
            provider_profile_id="deepseek", model_id="deepseek-v4-pro",
            route_id="deepseek.anthropic_web", route_role="native_search",
            protocol="anthropic_messages", adapter_id="deepseek.anthropic_web",
            capability_signature=("text", "native_web_search"),
            route_manifest_version="unified-invocation-routes-v3",
            route_config_fingerprint="search-v3", capability_status="declared",
        ),
    })
    plan = ExecutionPlanner().plan(request(
        selection_mode="fixed", fixed_key=("deepseek", "deepseek-v4-pro"),
        coordinator_key=("deepseek", "deepseek-v4-pro"),
        requirements=CapabilityRequirements(
            web="required", files="write", tools="required",
        ),
        candidates=(primary, search), web_route="specialist",
    ))
    assert [(step.step_type, step.invocation_route_id) for step in plan.steps] == [
        ("web_research", "deepseek.anthropic_web"),
        ("final_response", "deepseek.openai_chat"),
    ]


def test_route_resolver_uses_exact_tested_signatures_instead_of_route_superset() -> None:
    base = candidate("openai", "model", vision=True, local=False, native_search=True)
    route = {
        "route_id": "openai.responses", "route_role": "primary",
        "protocol": "openai_responses", "adapter_id": "openai_responses_web_search",
        "manifest_version": "invocation-routes-v2", "config_fingerprint": "fingerprint",
        "capabilities": ["text", "vision", "function_tools", "native_web_search"],
    }
    states = (
        {"route_id": "openai.responses", "capability_signature": ["text"],
         "status": "declared", "route_config_fingerprint": "fingerprint"},
        {"route_id": "openai.responses",
         "capability_signature": ["native_web_search", "text"],
         "status": "verified", "route_config_fingerprint": "fingerprint"},
    )
    candidates = InvocationRouteResolver.enumerate(
        base, (route,), states, legacy_native_status="declared", legacy_native_search=None,
    )
    assert {item.invocation_target.capability_signature for item in candidates} == {
        ("text",), ("native_web_search", "text"),
    }
    plan = ExecutionPlanner().plan(request(
        selection_mode="fixed", fixed_key=("openai", "model"),
        requirements=CapabilityRequirements(web="required"), candidates=candidates,
        web_route="native",
    ))
    assert plan.steps[-1].capability_signature == ("native_web_search", "text")


def test_persisted_deepseek_route_binds_its_own_adapter_and_endpoint() -> None:
    profile = ProviderProfile(
        id="deepseek", name="DeepSeek", kind="openai_compatible",
        base_url="https://api.deepseek.com", api_key="secret",
        allowed_model_ids=("deepseek-v4-pro",), primary_protocol="openai_chat",
        native_search={"adapter_id": "none", "protocol": "none"},
        invocation_routes=({
            "route_id": "deepseek.anthropic_web", "route_role": "native_search",
            "protocol": "anthropic_messages",
            "adapter_id": "anthropic_messages_web_search",
            "endpoint_base_url": "https://api.deepseek.com/anthropic",
            "source": "official_preset", "manifest_version": "invocation-routes-v2",
        },),
    )
    bound = ChatRunService._profile_for_step(profile, {
        "invocation_route_id": "deepseek.anthropic_web",
        "protocol_id": "anthropic_messages",
    })
    assert bound.base_url == "https://api.deepseek.com/anthropic"
    assert bound.native_search_endpoint == "https://api.deepseek.com/anthropic"
    assert bound.native_web_adapter == "anthropic_messages_web_search"


def test_v13_to_v14_persists_exact_invocation_route_columns(tmp_path) -> None:
    path = tmp_path / "schema14.db"
    CoreRepository(path)
    with sqlite3.connect(path) as connection:
        step_columns = {row[1] for row in connection.execute("PRAGMA table_info(execution_steps)")}
        plan_columns = {row[1] for row in connection.execute("PRAGMA table_info(execution_plans)")}
    assert {"invocation_route_id", "adapter_id", "capability_signature_json",
            "route_config_fingerprint"}.issubset(step_columns)
    assert {"route_catalog_hash", "route_policy_version"}.issubset(plan_columns)


def test_switching_back_to_automatic_clears_legacy_fixed_selection(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "routing-mode.db")
    conversation = repository.create_conversation("standard_chat", "Chat", "casual")
    fixed = repository.update_conversation_routing(
        conversation["id"], "fixed", "quality", "provider", "model",
    )
    assert fixed["provider_profile_id"] == "provider"
    automatic = repository.update_conversation_routing(
        conversation["id"], "automatic", "balanced",
    )
    assert automatic["provider_profile_id"] is None
    assert automatic["model_id"] is None
    assert automatic["coordinator_provider_profile_id"] is None


def test_automatic_routing_can_replace_and_reset_the_real_coordinator(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "automatic-coordinator.db")
    conversation = repository.create_conversation("standard_chat", "Chat", "casual")

    changed = repository.update_conversation_routing(
        conversation["id"], "automatic", "balanced", "deepseek", "deepseek-v4-pro",
    )
    assert changed["model_selection_mode"] == "automatic"
    assert changed["provider_profile_id"] is None
    assert changed["model_id"] is None
    assert changed["coordinator_provider_profile_id"] == "deepseek"
    assert changed["coordinator_model_id"] == "deepseek-v4-pro"
    assert changed["messages"][-1]["event_type"] == "coordinator_changed"

    reset = repository.update_conversation_routing(
        conversation["id"], "automatic", "balanced",
    )
    assert reset["coordinator_provider_profile_id"] is None
    assert reset["coordinator_model_id"] is None


def test_production_routing_configuration_excludes_demo_from_automatic_candidates(
    tmp_path,
) -> None:
    async def sink(_event):
        return None

    service = ChatRunService(CoreRepository(tmp_path / "production-routing.db"), sink)
    assert any(
        item.provider_profile_id == "builtin-demo"
        for item in service._execution_candidates(None, automatic_only=True)
    )

    service.configure_routing({
        "new_conversation_mode": "automatic",
        "default_preset": "balanced",
        "max_model_calls_per_message": 3,
        "max_specialist_steps": 2,
        "max_extra_online_cost_usd": "0.20",
        "allow_unknown_price_automatic": False,
    })

    assert all(
        item.provider_profile_id != "builtin-demo"
        for item in service._execution_candidates(None, automatic_only=True)
    )
