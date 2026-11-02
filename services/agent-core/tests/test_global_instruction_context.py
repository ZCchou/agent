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
