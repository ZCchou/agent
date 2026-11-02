"""Compatibility façade for Context Engine v2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.context_engine.policy import estimate_tokens
from agent.context_engine.service import ContextEngineService
from agent.storage import CoreDataError, CoreRepository


@dataclass(frozen=True)
class AssembledContext:
    messages: list[dict[str, Any]]
    items: list[dict[str, Any]]
    estimated_tokens: int
    token_budget: int
    content_hash: str
    context_window: int
    output_reserve: int
    stable_prefix_hash: str
    dynamic_suffix_hash: str
    context_generation: int


class ContextAssembler:
    VERSION = ContextEngineService.VERSION

    def __init__(self, repository: CoreRepository) -> None:
        self.engine = ContextEngineService(repository)

    def assemble(
        self, run: dict[str, Any], *, token_budget: int = 8_000,
        actor: str = "user", capabilities: frozenset[str] = frozenset(),
        context_window: int | None = None, max_output_tokens: int | None = None,
    ) -> AssembledContext:
        del capabilities
        if actor != "user":
            raise CoreDataError("unsupported_initiator", "Only user-initiated runs are currently supported.")
        packed = self.engine.assemble(
            run, context_window=context_window or token_budget, max_output_tokens=max_output_tokens,
        )
        return AssembledContext(
            messages=packed.messages,
            items=[item.to_record() for item in packed.items],
            estimated_tokens=packed.estimated_tokens,
            token_budget=packed.input_target,
            content_hash=packed.content_hash,
            context_window=packed.context_window,
            output_reserve=packed.output_reserve,
            stable_prefix_hash=packed.stable_prefix_hash,
            dynamic_suffix_hash=packed.dynamic_suffix_hash,
            context_generation=packed.context_generation,
        )


__all__ = ["AssembledContext", "ContextAssembler", "estimate_tokens"]
