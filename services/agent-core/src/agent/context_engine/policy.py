from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from agent.context_engine.domain import ContextEngineError, ContextPolicy, CompactionTarget
from agent.context_engine.compaction import compaction_messages, checkpoint_schema
from agent.execution.output_budget import FALLBACK_CONTEXT_WINDOW, OutputBudget, resolve_output_budget


DEFAULT_POLICY = ContextPolicy(
    version="context-default-v5",
    target_ratio=0.80,
    hard_limit_ratio=0.85,
    fallback_context_window=FALLBACK_CONTEXT_WINDOW,
    minimum_output_reserve=512,
    token_method="conservative-char-v2",
    segmenter_version="paragraph-unicode-v1",
    # Reasoning-capable APIs count private reasoning and the final checkpoint
    # against the same completion limit. 1024 truncated the JSON before the
    # visible answer on real providers. JSON mode plus a concise schema bounds
    # the stored checkpoint; this ceiling bounds the complete provider work.
    compaction_output_limit=8_192,
    minimum_compaction_reclaim=512,
)


@dataclass(frozen=True)
class PostRunCompactionDecision:
    should_schedule: bool
    accumulated_tokens: int
    reclaimable_tokens: int
    input_target: int
    reason: str


class PostRunCompactionPolicy:
    """Pure post-run high-water check.

    ``input_target`` already excludes the output reserve.  Keeping the check here
    prevents callers from counting that reserve (or the current user message)
    for a second time.
    """

    @staticmethod
    def evaluate(
        *, snapshot_tokens: int, assistant_tokens: int, input_target: int,
        reclaimable_tokens: int, minimum_reclaim: int = DEFAULT_POLICY.minimum_compaction_reclaim,
    ) -> PostRunCompactionDecision:
        accumulated = max(0, snapshot_tokens) + max(0, assistant_tokens)
        reclaimable = max(0, reclaimable_tokens)
        if reclaimable < minimum_reclaim:
            return PostRunCompactionDecision(
                False, accumulated, reclaimable, input_target, "insufficient_reclaim",
            )
        if accumulated < input_target:
            return PostRunCompactionDecision(
                False, accumulated, reclaimable, input_target, "below_input_target",
            )
        return PostRunCompactionDecision(
            True, accumulated, reclaimable, input_target, "input_target_reached",
        )


def estimate_tokens(value: Any) -> int:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # Conservative for mixed Chinese/English text without a provider tokenizer.
    return max(1, math.ceil(len(value) / 3))


def resolve_budget(
    *, context_window: int | None, max_output_tokens: int | None,
    policy: ContextPolicy = DEFAULT_POLICY, expanded: bool = False, strict: bool = False,
) -> tuple[int, int, int, int]:
    window = context_window if isinstance(context_window, int) and context_window > 0 \
        else policy.fallback_context_window
    output_reserve = max(
        policy.minimum_output_reserve,
        max_output_tokens if isinstance(max_output_tokens, int) and not isinstance(max_output_tokens, bool)
        and max_output_tokens > 0 else resolve_output_budget(
            "final_response", model_limit=None, context_window=context_window,
        ).reservation_tokens,
    )
    hard_limit = max(1, int(window * policy.hard_limit_ratio) - output_reserve)
    target_ratio = 0.65 if strict else 0.90 if expanded else policy.target_ratio
    input_target = max(1, int(hard_limit * target_ratio))
    if input_target <= 0 or hard_limit <= 0:
        raise ContextEngineError("context_policy_invalid", "The model context policy leaves no input space.")
    return window, input_target, output_reserve, hard_limit


@dataclass(frozen=True)
class CompactionRequest:
    messages: list[dict[str, str]]
    output_budget: OutputBudget
    estimated_input_tokens: int
    safety_margin_tokens: int
    sources: list[dict[str, Any]]
    through_sequence: int
    through_offset: int | None


def resolve_compaction_request(
    sources: list[dict[str, Any]], checkpoint: dict[str, Any] | None,
    target: CompactionTarget, *, correction: str | None = None,
    protocol_limit: int | None = None,
) -> CompactionRequest:
    """Budget the complete JSON message envelope, not just source text.

    UTF-8 bytes / 3 plus a window-scaled margin is a conservative estimate,
    not a provider tokenizer. Corrections retain exactly the claimed sources.
    """
    window = target.context_window if target.context_window is not None else DEFAULT_POLICY.fallback_context_window
    margin = max(128, math.ceil(window * 0.05))
    compact_format = target.protocol_id in {'openai_chat', 'ollama', 'ollama_chat'}

    def exhausted():
        return ContextEngineError("context_compaction_budget_exhausted", "No history segment fits the complete compaction request budget.")

    def prompt(entries):
        messages = compaction_messages(entries, compact_format=compact_format)
        if correction is not None:
            messages += [{"role": "assistant", "content": correction}, {"role": "user", "content": (
                "Correct the validation problem and return the JSON structure in json_example with no prose. "
                "Every item needs its text and source_ids. Cite only an "
                "allowed_source_id from the request."
            )}]
        return messages

    def cost(messages):
        # Include roles, JSON escaping, source IDs, schema and correction history.
        # A conservative superset of current text-only transport envelopes:
        # content blocks cover Responses/Anthropic expansion; both output key
        # spellings and JSON mode also cover the smaller chat/Ollama envelopes.
        envelope = {
            "model": target.model_id,
            "messages": [{"role": message["role"], "content": [
                {"type": "input_text", "text": message["content"]}
            ]} for message in messages],
            "stream": True, "store": False,
            "max_tokens": 8192, "max_output_tokens": 8192,
            "response_format": {"type": "json_object"},
            "options": {"num_predict": 8192},
        }
        if compact_format:
            entries = json.loads(messages[1]['content'])['sources']
            envelope['response_format'] = {'type': 'json_schema', 'json_schema': {
                'name': 'conversation_checkpoint', 'strict': True, 'schema': checkpoint_schema(entries)}}
        return math.ceil(len(json.dumps(envelope, ensure_ascii=False).encode("utf-8")) / 3)

    try:
        desired = resolve_output_budget("context_compaction", model_limit=target.max_output_tokens,
                                        protocol_limit=protocol_limit)
        if window <= 0:
            raise exhausted()
        selected = []
        if checkpoint:
            selected.append({"source_id": checkpoint["id"], "role": "checkpoint",
                             "trust": "model_inference", "content": checkpoint["content_json"]})
        through_sequence, through_offset = 0, None
        if correction is not None or (sources and "source_id" in sources[0]):
            # Already-selected sources are immutable for fallback/correction.
            selected = list(sources)
        else:
            available = window - margin - cost(prompt(selected))
            # Reserve output once; leave room for source progress even on small models.
            allowance = min(desired.initial_tokens, available // 2)
            if allowance <= 0:
                raise exhausted()
            for source in sources:
                text = source["content"]
                start = int(source.get("_source_start", 0))
                def entry(length):
                    end = start + length
                    identifier = source["id"] if start == 0 and length == len(text) else f'{source["id"]}:{start}:{end}'
                    return {"source_id": identifier, "message_id": source["id"], "role": source["role"],
                            "trust": "user_authored" if source["role"] == "user" else "model_inference",
                            "content": text[:length]}
                low, high = 0, len(text)
                while low < high:
                    middle = (low + high + 1) // 2
                    if cost(prompt(selected + [entry(middle)])) + allowance + margin <= window:
                        low = middle
                    else:
                        high = middle - 1
                if low == 0:
                    # Empty stored messages (e.g. attachment-only turns) are
                    # real provenance. Account for their envelope and advance
                    # past them; do not confuse emptiness with failure to fit.
                    if text or cost(prompt(selected + [entry(0)])) + allowance + margin > window:
                        break
                selected.append(entry(low))
                through_sequence = source.get("sequence_no") or through_sequence
                through_offset = start + low
                if low < len(text):
                    break
            if through_sequence < 1:
                raise exhausted()
        messages = prompt(selected)
        estimated = cost(messages)
        budget = resolve_output_budget("context_compaction", model_limit=target.max_output_tokens,
                                       protocol_limit=protocol_limit,
                                       remaining_context_tokens=window - margin - estimated)
        return CompactionRequest(messages, budget, estimated, margin, selected, through_sequence, through_offset)
    except ValueError as error:
        raise exhausted() from error
