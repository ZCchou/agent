"""Scoped negative controls at real context boundaries; never alter expectations."""
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

from agent.context_engine import service as service_module
from agent.context_engine.domain import TrustKind
from agent.repositories.context_repository import ContextRepository

REQUIRED_MUTATIONS = (
    "drop_history", "inject_foreign_source", "upgrade_external_trust",
    "drop_selected_attachment", "ignore_context_limit", "leave_compaction_running",
)


@contextmanager
def apply_mutation(name, *, foreign_ids):
    if name is None:
        yield
        return
    if name not in REQUIRED_MUTATIONS:
        raise ValueError(f"Unknown mutation: {name}")
    original_load = ContextRepository.load_context_sources
    original_pack = service_module.pack_context
    original_budget = service_module.resolve_budget

    def load(store, run_id, **kwargs):
        sources = original_load(store, run_id, **kwargs)
        messages = sources["conversation"]["messages"]
        current_id = sources["run"]["user_message_id"]
        if name == "drop_history":
            sources["conversation"]["messages"] = [m for m in messages if m["id"] == current_id]
        elif name == "inject_foreign_source":
            # Real rows from sibling work / another project simulate losing SQL scope.
            with store.core._connect() as connection:
                foreign = [dict(connection.execute("SELECT * FROM messages WHERE id=?", (identifier,)).fetchone())
                           for identifier in foreign_ids]
            sources["conversation"]["messages"] = foreign + messages
        elif name == "drop_selected_attachment":
            for message in messages:
                if message["id"] == current_id:
                    message["attachments"] = []
        return sources

    def pack(**kwargs):
        packed = original_pack(**kwargs)
        if any(item.source.source_type == "attachment" and item.included for item in packed.items):
            items = [replace(item, source=replace(item.source, trust=TrustKind.SYSTEM_POLICY))
                     if item.source.source_type == "attachment" else item for item in packed.items]
            messages = [dict(message) for message in packed.messages]
            messages[-1]["role"] = "system"
            return replace(packed, messages=messages, items=items)
        return packed

    def unlimited_budget(**kwargs):
        kwargs["context_window"] = 1_000_000
        return original_budget(**kwargs)

    if name in {"drop_history", "inject_foreign_source", "drop_selected_attachment"}:
        target = patch.object(ContextRepository, "load_context_sources", load)
    elif name == "upgrade_external_trust":
        target = patch.object(service_module, "pack_context", pack)
    elif name == "ignore_context_limit":
        target = patch.object(service_module, "resolve_budget", unlimited_budget)
    else:
        target = patch.object(ContextRepository, "fail_compaction", lambda *args, **kwargs: False)
    with target:
        yield
