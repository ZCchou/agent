"""Core-owned update readiness, serialized by the Core's async dispatcher."""
from agent.storage import CoreDataError

ADMISSION_TYPES = frozenset({
    "model.run.start", "model.run.retry", "model.run.context.continue",
    "model.run.budget.resolve", "context.compaction.resume",
    "provider.native_search.probe", "model.tool_capability.probe",
})

MANAGEMENT_READ_ACTIONS = {
    "local_model.request": frozenset({"model.list", "job.list", "runtime.status"}),
    "rag.request": frozenset({"library.list", "document.list", "document.preview", "job.list",
                              "model.list", "evidence.read"}),
}


def is_admission(message: dict) -> bool:
    kind = message.get("type")
    if kind in MANAGEMENT_READ_ACTIONS:
        return message.get("params", {}).get("action") not in MANAGEMENT_READ_ACTIONS[kind]
    return kind in ADMISSION_TYPES


def prepare(service, update_id: str) -> dict:
    if len(update_id) > 100:
        raise CoreDataError("invalid_params", "Invalid update identity.")
    if service.update_prepared_id not in {None, update_id}:
        raise CoreDataError("app_update_busy", "Another update is being prepared.")
    state = service.repository.app_update_readiness()
    state["active_admissions"] = service.update_admissions
    state["active_tasks"] = sum(not task.done() for task in service.run_tasks.values())
    state["active_compactions"] = sum(not task.done() for task in service.compaction_tasks.values())
    if any(state.values()):
        return {"ready": False, **state}
    # No await between readiness, checkpoint and admission closure.
    service.repository.flush_for_app_update()
    service.update_prepared_id = update_id
    return {"ready": True, **state}


def abort(service, update_id: str) -> dict:
    if service.update_prepared_id not in {None, update_id}:
        raise CoreDataError("app_update_identity_mismatch", "A different update owns preparation.")
    service.update_prepared_id = None
    return {"resumed": True}
