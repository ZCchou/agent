from __future__ import annotations

import json

from agent.context_engine.domain import (
    ContextItem,
    ContextSection,
    PackedContext,
    Representation,
    RetrievalConfidence,
    SourceRef,
    TrustKind,
)
from agent.context_engine.policy import DEFAULT_POLICY
from agent.repositories.context_repository import ContextRepository
from agent.storage import CoreRepository


def test_rebudgeting_keeps_complete_input_limit_separate_from_body_limit(tmp_path):
    from agent.context_engine.service import ContextEngineService
    core = CoreRepository(tmp_path / 'budget.db')
    run = core.start_model_run(content='hello', idempotency_key='budget', provider_profile_id='p',
                               model_id='m', kind='standard_chat', category='casual')
    service = ContextEngineService(core)
    packed = service.assemble(run, context_window=8192, max_output_tokens=2048,
                              serialization_identity={'input_overhead_tokens': 770})
    assert packed.input_hard_limit == 4915
    assert packed.body_input_limit == 4145
    service.persist_snapshot(run['id'], 0, packed)
    snapshot = service.context_repository.preview(run['id'])
    assert snapshot['input_hard_limit'] == 4915 and snapshot['body_input_limit'] == 4145


def test_final_snapshot_records_complete_request_and_corrects_legacy_denominator(tmp_path):
    from agent.context_engine.service import ContextEngineService
    core = CoreRepository(tmp_path / 'final-budget.db')
    run = core.start_model_run(content='hello', idempotency_key='final-budget', provider_profile_id='p',
                               model_id='m', kind='standard_chat', category='casual')
    service = ContextEngineService(core)
    packed = service.assemble(run, context_window=8192, max_output_tokens=2048)
    service.persist_snapshot(run['id'], 0, packed)
    with core._connect() as connection:
        connection.execute('UPDATE context_snapshots SET policy_json=? WHERE run_id=?',
            (json.dumps({'input_hard_limit': 4145, 'hard_limit_ratio': 0.85}), run['id']))
    assert service.context_repository.preview(run['id'])['input_hard_limit'] == 4915
    budget = {'context_window': 8192, 'output_reserve': 2048, 'input_hard_limit': 4915,
              'request_tokens': 4500, 'message_tokens': 4200, 'input_overhead_tokens': 300,
              'remaining_input_tokens': 415}
    service.context_repository.save_tool_round_snapshot(run['id'], 1, [{'role': 'user', 'content': 'hello'}], request_budget=budget)
    snapshot = service.context_repository.preview(run['id'])
    assert snapshot['estimated_tokens'] == 4500 and snapshot['input_hard_limit'] == 4915
    assert snapshot['input_scope'] == 'compiled_request'
    assert snapshot['request_constraint_tokens'] == 300


def test_context_snapshot_audits_global_revision_and_previews_global_source(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "global-context.db")
    run = repository.start_model_run(
        content="hello", idempotency_key="global-context-run", provider_profile_id="p",
        model_id="m", kind="standard_chat", category="casual",
    )
    instructions = repository.get_global_instructions()
    item = ContextItem(
        source=SourceRef(
            "global_instructions", "1", TrustKind.USER_AUTHORED,
            source_hash=instructions["content_hash"], source_version=str(instructions["revision"]),
        ),
        section=ContextSection.STABLE, representation=Representation.ORIGINAL,
        estimated_tokens=10, included=True,
    )
    packed = PackedContext(
        messages=[{"role": "system", "content": "global"}], items=[item],
        estimated_tokens=10, context_window=1000, input_target=800, output_reserve=200,
        stable_prefix_hash="stable", dynamic_suffix_hash="dynamic", content_hash="content",
        retrieval_confidence=RetrievalConfidence.NONE, context_generation=0,
        global_instruction_revision=instructions["revision"],
        global_instruction_hash=instructions["content_hash"],
    )
    context = ContextRepository(repository)
    snapshot_id = context.save_snapshot(
        run["id"], 0, packed, json.dumps({"version": DEFAULT_POLICY.version}),
    )

    snapshot = context.preview(run["id"])
    assert snapshot["global_instruction_revision"] == instructions["revision"]
    assert snapshot["global_instruction_hash"] == instructions["content_hash"]
    source_item_id = next(item["id"] for item in snapshot["items"] if item["source_type"] == "global_instructions")
    source = context.source_preview(run["id"], source_item_id)
    assert source["source_type"] == "global_instructions"
    assert source["source"]["revision"] == instructions["revision"]
    assert snapshot["id"] == snapshot_id
