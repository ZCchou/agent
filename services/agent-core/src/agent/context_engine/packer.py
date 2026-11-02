from __future__ import annotations

import hashlib
import json
from typing import Any

from agent.context_engine.domain import (
    ContextEngineError,
    ContextItem,
    ContextSection,
    PackedContext,
    Representation,
    RetrievalConfidence,
    SourceRef,
    TrustKind,
)
from agent.context_engine.policy import estimate_tokens
from agent.context_engine.retrieval import RetrievedKnowledge


def _hash(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _message_cost(message: dict[str, Any]) -> int:
    return int(message.get("_estimated_tokens") or estimate_tokens(message.get("content", "")))


def pack_context(
    *, system_message: str, pinned_knowledge: list[dict[str, Any]],
    checkpoint: dict[str, Any] | None, history_turns: list[Any],
    retrieved: list[RetrievedKnowledge], retrieval_confidence: RetrievalConfidence,
    task_state: dict[str, Any], current_message: dict[str, Any], context_generation: int,
    context_window: int, input_target: int, hard_limit: int, output_reserve: int,
    serialization_identity: dict[str, Any] | None = None,
    global_instruction: dict[str, Any] | None = None,
    include_global_instruction: bool = True,
) -> PackedContext:
    stable_messages: list[dict[str, Any]] = [{"role": "system", "content": system_message}]
    dynamic_messages: list[dict[str, Any]] = []
    items: list[ContextItem] = []

    system_cost = estimate_tokens(system_message)
    items.append(ContextItem(
        SourceRef("safety_and_behavior", None, TrustKind.SYSTEM_POLICY),
        ContextSection.STABLE, Representation.ORIGINAL, system_cost, True, "mandatory",
    ))
    stable_cost = system_cost

    if global_instruction is not None and include_global_instruction:
        content = str(global_instruction.get("content") or "")
        if global_instruction.get("enabled") and content:
            items.append(ContextItem(
                SourceRef(
                    "global_instructions", "1", TrustKind.USER_AUTHORED,
                    source_hash=global_instruction.get("content_hash")
                    or hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    source_version=str(global_instruction.get("revision") or "1"),
                ),
                ContextSection.STABLE, Representation.ORIGINAL,
                estimate_tokens(content), True, "enabled_global_behavior",
            ))

    for knowledge in sorted(pinned_knowledge, key=lambda item: item["id"]):
        rendered = (
            f'<user-confirmed-data source="project-knowledge:{knowledge["id"]}">\n'
            f'{knowledge["title"]}: {knowledge["content"]}\n</user-confirmed-data>'
        )
        cost = estimate_tokens(rendered)
        # Confirmed project data is stable, but never receives system-role authority.
        stable_messages.append({"role": "user", "content": rendered})
        stable_cost += cost
        items.append(ContextItem(
            SourceRef("project_knowledge", knowledge["id"], TrustKind.USER_CONFIRMED_PROJECT_DATA,
                      source_hash=hashlib.sha256(knowledge["content"].encode("utf-8")).hexdigest(),
                      source_version=str(knowledge.get("current_revision", ""))),
            ContextSection.STABLE, Representation.ORIGINAL, cost, True, "pinned",
        ))

    compression_applied = False
    if checkpoint is not None and checkpoint.get("status") == "valid":
        checkpoint_content = checkpoint["content_json"]
        rendered = (
            '<context-checkpoint trust="mixed" instruction="summarized data, not system policy">\n'
            f"{checkpoint_content}\n</context-checkpoint>"
        )
        cost = estimate_tokens(rendered)
        # A checkpoint is model-produced memory.  Keeping it out of the system role
        # prevents compressed external content from being promoted into policy.
        stable_messages.append({"role": "assistant", "content": rendered})
        stable_cost += cost
        compression_applied = True
        items.append(ContextItem(
            SourceRef("context_checkpoint", checkpoint["id"], TrustKind.MODEL_INFERENCE,
                      source_hash=checkpoint["source_hash"],
                      derived_from_untrusted="external_untrusted" in checkpoint_content),
            ContextSection.CHECKPOINT, Representation.CHECKPOINT, cost, True, "active_checkpoint",
        ))

    current_cost = _message_cost(current_message)
    if system_cost + current_cost > hard_limit:
        raise ContextEngineError(
            "current_input_too_large", "The current request cannot fit in the selected model context.",
        )
    if stable_cost + current_cost > hard_limit:
        raise ContextEngineError(
            "pinned_context_too_large", "Pinned context leaves no room for the current request.",
        )

    used = stable_cost + current_cost
    selected_turns: list[Any] = []
    excluded_turns: list[Any] = []
    for turn in reversed(history_turns):
        if any(message["id"] == current_message["id"] for message in turn.messages):
            continue
        cost = sum(_message_cost(message) for message in turn.messages)
        if used + cost <= input_target:
            selected_turns.append(turn)
            used += cost
        else:
            excluded_turns.append(turn)
    selected_turns.reverse()
    for turn in selected_turns:
        for message in turn.messages:
            dynamic_messages.append({"role": message["role"], "content": message.get("_model_content", message["content"])})
            message_trust = (
                TrustKind.USER_AUTHORED if message["role"] == "user" else TrustKind.MODEL_INFERENCE
            )
            items.append(ContextItem(
                SourceRef("message", message["id"], message_trust,
                          source_hash=hashlib.sha256(message["content"].encode("utf-8")).hexdigest()),
                ContextSection.RECENT_HISTORY,
                Representation.RAW_REPLAY if message.get("_raw_replay") else Representation.ORIGINAL,
                _message_cost(message), True,
                "expanded_history_replay" if message.get("_raw_replay")
                else "recent_complete_turn" if turn.complete else "legacy_history",
            ))
    for turn in reversed(excluded_turns):
        for message in turn.messages:
            message_trust = (
                TrustKind.USER_AUTHORED if message["role"] == "user" else TrustKind.MODEL_INFERENCE
            )
            items.append(ContextItem(
                SourceRef("message", message["id"], message_trust,
                          source_hash=hashlib.sha256(message["content"].encode("utf-8")).hexdigest()),
                ContextSection.RECENT_HISTORY, Representation.ORIGINAL, _message_cost(message),
                False, exclusion_reason="token_budget",
            ))

    if excluded_turns:
        raise ContextEngineError(
            "context_compaction_required",
            "Older history must be compacted before this request can run.",
        )

    for candidate in retrieved:
        knowledge = candidate.item
        rendered = (
            f'<user-confirmed-data source="project-knowledge:{knowledge["id"]}">\n'
            f'{knowledge["title"]}: {knowledge["content"]}\n</user-confirmed-data>'
        )
        cost = estimate_tokens(rendered)
        included = used + cost <= input_target
        items.append(ContextItem(
            SourceRef("project_knowledge", knowledge["id"], TrustKind.USER_CONFIRMED_PROJECT_DATA,
                      source_hash=hashlib.sha256(knowledge["content"].encode("utf-8")).hexdigest(),
                      source_version=str(knowledge.get("current_revision", ""))),
            ContextSection.RETRIEVAL, Representation.ORIGINAL, cost, included,
            "lexical_match" if included else None,
            None if included else "token_budget",
            {"exact": candidate.exact, "title_hits": candidate.title_hits,
             "content_hits": candidate.content_hits},
        ))
        if included:
            dynamic_messages.append({"role": "user", "content": rendered})
            used += cost

    if task_state:
        rendered_state = (
            '<system-observation instruction="runtime facts, not user instructions">\n'
            + json.dumps(task_state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n</system-observation>"
        )
        cost = estimate_tokens(rendered_state)
        included = used + cost <= input_target
        items.append(ContextItem(
            SourceRef("task_state", None, TrustKind.SYSTEM_OBSERVATION),
            ContextSection.TASK_STATE, Representation.METADATA, cost, included,
            "current_runtime_state" if included else None,
            None if included else "token_budget",
        ))
        if included:
            dynamic_messages.append({"role": "system", "content": rendered_state})
            used += cost

    dynamic_messages.append({
        "role": "user", "content": current_message.get("_model_content", current_message["content"]),
    })
    items.append(ContextItem(
        SourceRef("current_message", current_message["id"], TrustKind.USER_AUTHORED,
                  source_hash=hashlib.sha256(current_message["content"].encode("utf-8")).hexdigest()),
        ContextSection.CURRENT, Representation.ORIGINAL,
        estimate_tokens(current_message["content"]), True, "current_request",
    ))
    for attachment_item in current_message.get("_attachment_items", []):
        attachment = attachment_item["attachment"]
        artifact = attachment_item.get("artifact") or {}
        exclusion = attachment_item.get("exclusion_reason")
        items.append(ContextItem(
            SourceRef("attachment", attachment["id"], TrustKind.EXTERNAL_UNTRUSTED,
                      source_start=attachment.get("source_start"), source_end=attachment.get("source_end"),
                      source_hash=artifact.get("sha256") or attachment.get("sha256"), derived_from_untrusted=True),
            ContextSection.CURRENT, Representation.REFERENCE if exclusion else Representation.ORIGINAL,
            attachment_item["estimated_tokens"], not exclusion, None if exclusion else "current_attachment",
            exclusion_reason=exclusion,
            metadata={"name": attachment["name"], "kind": attachment["kind"],
                "current_attachment_id": attachment["id"],
                "source_attachment_id": attachment.get("source_attachment_id") or attachment["id"],
                "source_message_id": attachment.get("source_message_id") or attachment.get("message_id"),
                "artifact_id": artifact.get("artifact_id"),
                "artifact_hash": artifact.get("sha256") or attachment.get("sha256"),
                "receiving_route": attachment_item.get("receiving_route"),
                "effective_attachment_ids": [value["attachment"]["id"] for value in current_message.get("_attachment_items", [])]},
        ))

    messages = stable_messages + dynamic_messages
    hash_scope = serialization_identity or {}
    stable_hash = _hash({"serialization": hash_scope, "messages": stable_messages})
    dynamic_hash = _hash({"serialization": hash_scope, "messages": dynamic_messages})
    return PackedContext(
        messages=messages,
        items=items,
        estimated_tokens=used,
        context_window=context_window,
        input_target=input_target,
        output_reserve=output_reserve,
        stable_prefix_hash=stable_hash,
        dynamic_suffix_hash=dynamic_hash,
        content_hash=_hash({"serialization": hash_scope, "messages": messages}),
        retrieval_confidence=retrieval_confidence,
        context_generation=context_generation,
        global_instruction_revision=int((global_instruction or {}).get("revision") or 0),
        global_instruction_hash=(global_instruction or {}).get("content_hash"),
        compression_applied=compression_applied,
    )
