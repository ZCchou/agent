from __future__ import annotations

from agent.execution.output_budget import (
    POLICY_VERSION,
    is_zero_output_budget_exhaustion,
    resolve_output_budget,
)


def test_vision_budget_respects_model_ceiling_and_has_bounded_escalation() -> None:
    generous = resolve_output_budget("vision_analysis", model_limit=16_384)
    assert generous.initial_tokens == 4_096
    assert generous.retry_tokens == 8_192
    assert generous.reservation_tokens == 8_192
    assert generous.maximum_billed_output_tokens == 12_288
    assert generous.model_limit == 16_384
    assert generous.policy_version == POLICY_VERSION

    constrained = resolve_output_budget("vision_analysis", model_limit=3_000)
    assert constrained.initial_tokens == 3_000
    assert constrained.retry_tokens is None
    assert constrained.reservation_tokens == 3_000
    assert constrained.maximum_billed_output_tokens == 3_000


def test_all_specialist_purposes_share_the_same_model_ceiling() -> None:
    assert resolve_output_budget("web_research", model_limit=2_500).initial_tokens == 2_500
    assert resolve_output_budget("reasoning_analysis", model_limit=6_000).initial_tokens == 6_000
    assert resolve_output_budget("intent_analysis", model_limit=16_384).initial_tokens == 256
    assert resolve_output_budget("final_response", model_limit=16_384, context_window=65_536).initial_tokens == 16_384


def test_protocol_and_context_limits_can_only_reduce_the_budget() -> None:
    budget = resolve_output_budget(
        "vision_analysis",
        model_limit=16_384,
        protocol_limit=6_000,
        remaining_context_tokens=5_000,
    )
    assert budget.initial_tokens == 4_096
    assert budget.retry_tokens == 5_000


def test_only_zero_text_output_limit_exhaustion_is_safe_to_escalate() -> None:
    assert is_zero_output_budget_exhaustion(
        "provider_response_incomplete",
        {"incomplete_reason": "max_output_tokens", "text_character_count": 0},
    )
    assert not is_zero_output_budget_exhaustion(
        "provider_response_incomplete",
        {"incomplete_reason": "max_output_tokens", "text_character_count": 12},
    )
    assert not is_zero_output_budget_exhaustion(
        "provider_refusal",
        {"incomplete_reason": "max_output_tokens", "text_character_count": 0},
    )

import pytest

@pytest.mark.parametrize('field', ['model_limit', 'protocol_limit', 'remaining_context_tokens'])
@pytest.mark.parametrize('value', [0, -1])
def test_known_exhausted_ceiling_is_not_unknown(field, value):
    values = {'model_limit': 8192, field: value}
    with pytest.raises(ValueError):
        resolve_output_budget('final_response', **values)

def test_compaction_has_ceiling_without_escalation():
    budget = resolve_output_budget('context_compaction', model_limit=32768, protocol_limit=700)
    assert budget.initial_tokens == 700
    assert budget.retry_tokens is None


@pytest.mark.parametrize("window,model,protocol,remaining,expected", [
    (1_048_576, None, None, None, 16_384),
    (65_536, None, None, None, 16_384),
    (65_535, None, None, None, 4_096),
    (8_192, None, None, None, 2_048),
    (None, None, None, None, 2_048),
    (False, None, None, None, 2_048),
    (1_048_576, 128, None, None, 128),
    (1_048_576, 2_048, None, None, 2_048),
    (8_192, 32_768, None, None, 2_048),
    (1_048_576, None, 6_000, None, 6_000),
    (1_048_576, None, None, 3_000, 3_000),
])
def test_final_generation_preserves_headroom_and_explicit_ceilings(window, model, protocol, remaining, expected):
    budget = resolve_output_budget("final_response", model_limit=model,
        context_window=window, protocol_limit=protocol, remaining_context_tokens=remaining)
    assert budget.initial_tokens == budget.reservation_tokens == expected
    assert budget.retry_tokens is None


@pytest.mark.parametrize("purpose,window,model,reservation", [
    ("reasoning_analysis", 8_192, None, 4_096),
    ("vision_analysis", 16_384, 16_384, 8_192),
])
def test_specialist_packing_reserves_the_compilers_largest_allowed_response(purpose, window, model, reservation):
    from agent.context_engine.policy import resolve_budget
    from agent.execution.compiler import InvocationCompiler
    from test_unified_invocation_v3 import model_spec

    budget = resolve_output_budget(purpose, model_limit=model)
    _, _, reserve, hard_input_limit = resolve_budget(
        context_window=window, max_output_tokens=budget.reservation_tokens)
    for emitted in (budget.initial_tokens, budget.retry_tokens):
        if emitted is None:
            continue
        prepared = InvocationCompiler().compile_model(
            model_spec(max_output_tokens=emitted), [{"role": "user", "content": "Synthetic request"}])
        assert reserve >= prepared.options.max_output_tokens
        assert hard_input_limit + prepared.options.max_output_tokens <= window
    assert reserve == reservation
