from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from agent.execution.domain import (
    CapabilityRequirements,
    ExecutionBudget,
    InvocationSpec,
    InvocationTarget,
    ModelCandidate,
)
from agent.execution.compiler import InvocationCompiler
from agent.execution.planner import ExecutionPlanner, PlanningError, PlanningRequest
from agent.provider_tool_adapter import structured_messages
from agent.context_engine.domain import (
    ContextItem, ContextSection, PackedContext, Representation, RetrievalConfidence,
    SourceRef, TrustKind,
)
from agent.repositories.context_repository import ContextRepository
from agent.storage import CoreRepository
from agent.storage import CoreDataError
from agent.execution.retry import should_retry_transient_generation


@pytest.mark.parametrize("error_code", ["request_timeout", "stream_stalled", "connection_reset", "provider_unavailable"])
def test_transient_generation_retry_allows_clean_attempts(error_code: str) -> None:
    assert should_retry_transient_generation(
        error_code=error_code,
        produced_output=False,
        tool_names=(),
        retry_count=0,
    ) is True


def test_transient_generation_retry_allows_read_only_project_tools() -> None:
    assert should_retry_transient_generation(
        error_code="stream_stalled",
        produced_output=False,
        tool_names=("fs.list", "fs.read"),
        retry_count=1,
    ) is True


def test_transient_generation_retry_never_replays_side_effect_tools() -> None:
    assert should_retry_transient_generation(
        error_code="request_timeout",
        produced_output=False,
        tool_names=("fs.propose_changes",),
        retry_count=0,
    ) is False


def test_transient_generation_retry_stops_after_two_retries() -> None:
    assert should_retry_transient_generation(
        error_code="provider_unavailable",
        produced_output=False,
        tool_names=(),
        retry_count=2,
    ) is False


def _kimi_candidate() -> ModelCandidate:
    target = InvocationTarget(
        provider_profile_id="kimi",
        model_id="kimi-k3",
        route_id="kimi.openai_chat",
        route_role="native_search",
        protocol="openai_chat",
        adapter_id="kimi.openai_chat",
        capability_signature=("text", "native_web_search"),
        route_manifest_version="v4",
        route_config_fingerprint="kimi-v4",
        source_provenance="opaque",
        capability_status="verified",
    )
    return ModelCandidate(
        provider_profile_id="kimi",
        model_id="kimi-k3",
        protocol_id="openai_chat",
        location="online",
        capabilities={"text": "yes", "context_length": 32768},
        native_search_status="verified",
        estimated_input_cost_per_million=Decimal("1"),
        estimated_output_cost_per_million=Decimal("2"),
        invocation_target=target,
    )


def test_opaque_search_is_a_valid_fallback_when_no_structured_route_exists() -> None:
    candidate = _kimi_candidate()
    route = ExecutionPlanner().resolve_web_route(
        requirements=CapabilityRequirements(web="required", web_evidence="required"),
        candidates=(candidate,),
        coordinator_key=candidate.key,
        selection_mode="fixed",
        host_backend_id=None,
        host_backend_kind=None,
        request_mode="force_once",
    )

    assert route.route == "specialist"
    assert route.reason_code == "same_model_opaque_search_fallback"

    plan = ExecutionPlanner().plan(PlanningRequest(
        selection_mode="fixed", routing_preset="balanced",
        requirements=CapabilityRequirements(web="required", web_evidence="required"),
        candidates=(candidate,), budget=ExecutionBudget(), fixed_key=candidate.key,
        coordinator_key=candidate.key, web_route=route.route,
    ))
    assert plan.steps[0].step_type == "web_research"
    assert plan.steps[0].invocation_route_id == "kimi.openai_chat"


def test_automatic_search_prefers_authorized_structured_route_over_opaque_coordinator() -> None:
    kimi = _kimi_candidate()
    deepseek_target = replace(
        kimi.invocation_target,
        provider_profile_id="deepseek",
        model_id="deepseek-pro",
        route_id="deepseek.anthropic_web",
        route_role="native_search",
        protocol="anthropic_messages",
        adapter_id="deepseek.anthropic_web",
        route_config_fingerprint="deepseek-v4",
        source_provenance="structured",
    )
    deepseek = replace(
        kimi,
        provider_profile_id="deepseek",
        model_id="deepseek-pro",
        invocation_target=deepseek_target,
        native_search_status="declared",
    )

    route = ExecutionPlanner().resolve_web_route(
        requirements=CapabilityRequirements(web="required", web_evidence="required"),
        candidates=(kimi, deepseek),
        coordinator_key=kimi.key,
        selection_mode="automatic",
        host_backend_id=None,
        host_backend_kind=None,
        request_mode="auto",
    )

    assert route.reason_code == "authorized_native_search_specialist"


def test_structured_tool_prompt_contains_complete_contract_schema() -> None:
    messages = structured_messages([{"role": "user", "content": "list files"}], {"fs.list"})
    instruction = messages[0]["content"]
    assert "tool-contract-v2" in instruction
    assert '"name":"fs.list"' in instruction
    assert '"required":["path"]' in instruction
    assert '"additionalProperties":false' in instruction
    assert "project-dir:" in instruction


def test_invocation_spec_hash_includes_tool_transport_and_schema_hash() -> None:
    values = dict(
        spec_id="spec", plan_id="plan", step_id="step", round_no=1, attempt_no=1,
        kind="model", provider_or_backend_id="kimi", model_id="kimi-k3",
        route_id="kimi.openai_chat", protocol_id="openai_chat", adapter_id="kimi.openai_chat",
        endpoint_role="final_response", route_fingerprint="route-v4",
        capability_signature=("function_tools", "text"),
        allowed_tool_names=("fs.list",),
    )
    structured = InvocationSpec(**values, tool_transport="structured_json", tool_schema_hash="hash-v2")
    native = replace(structured, tool_transport="native_function_call", spec_hash="")
    assert structured.spec_hash != native.spec_hash
    assert structured.to_dict()["tools"]["transport"] == "structured_json"
    assert structured.to_dict()["tools"]["schema_hash"] == "hash-v2"


def test_compiler_injects_structured_schema_without_enabling_native_tools() -> None:
    spec = InvocationSpec(
        spec_id="spec", plan_id="plan", step_id="step", round_no=1, attempt_no=1,
        kind="model", provider_or_backend_id="kimi", model_id="kimi-k3",
        route_id="kimi.openai_chat", protocol_id="openai_chat", adapter_id="kimi.openai_chat",
        endpoint_role="final_response", route_fingerprint="route-v4",
        capability_signature=("function_tools", "text"),
        allowed_tool_names=("fs.list",), tool_transport="structured_json",
    )
    prepared = InvocationCompiler().compile_model(
        spec, [{"role": "user", "content": "list files"}],
    )
    assert prepared.tools_enabled is False
    assert prepared.tool_transport == "structured_json"
    assert "Complete allowed tool schemas" in prepared.messages[0]["content"]


def test_retry_reuses_identical_round_zero_context_snapshot(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "snapshot.db")
    run = repository.start_model_run(
        content="hello", idempotency_key="snapshot-run", provider_profile_id="p",
        model_id="m", kind="standard_chat", category="casual",
    )
    packed = PackedContext(
        messages=[{"role": "user", "content": "hello"}], items=[ContextItem(
            source=SourceRef("message", run["user_message_id"], TrustKind.USER_AUTHORED),
            section=ContextSection.CURRENT, representation=Representation.ORIGINAL,
            estimated_tokens=2, included=True,
        )], estimated_tokens=2, context_window=1000, input_target=800,
        output_reserve=200, stable_prefix_hash="stable", dynamic_suffix_hash="dynamic",
        content_hash="content", retrieval_confidence=RetrievalConfidence.HIGH,
        context_generation=0,
    )
    context = ContextRepository(repository)
    policy = json.dumps({"version": "test"})
    first = context.save_snapshot(run["id"], 0, packed, policy)
    second = context.save_snapshot(run["id"], 0, packed, policy)
    assert second == first
    changed = replace(packed, content_hash="different")
    with pytest.raises(CoreDataError, match="context snapshot differs"):
        context.save_snapshot(run["id"], 0, changed, policy)
