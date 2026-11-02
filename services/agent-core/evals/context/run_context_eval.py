from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass, field
import hashlib
import gc
import json
import os
from pathlib import Path
import sys
import threading
from tempfile import TemporaryDirectory
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for directory in (ROOT / "src", HERE):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from mutations import REQUIRED_MUTATIONS, apply_mutation
from agent.context_engine.service import ContextEngineService
from agent.context_engine.domain import ContextEngineError
from agent.providers import GenerationOptions, ProviderError, ProviderEvent
from agent.storage import CoreDataError, CoreRepository
from agent import storage as storage_module
from agent.repositories import context_repository as repository_module
from agent.runtime import ChatRunService


@dataclass
class EvalReport:
    suite: str
    cases: list[dict] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    deterministic: bool = True
    scope: str = "offline retention, provenance, selection and lifecycle; not model answer quality"


class FixtureProvider:
    """Capture assembled requests; compaction replies are authored fixtures."""
    def __init__(self, case, aliases):
        self.case, self.aliases = case, aliases
        self.calls = []

    async def stream_events(self, model, messages, *, tools, options):
        self.calls.append({"purpose": options.purpose, "messages": messages})
        if options.purpose == "context_compaction":
            if self.case.get("provider_error"):
                raise ProviderError(self.case["provider_error"], "Synthetic offline failure")
            payload = {key: [] for key in (
                "goal", "success_criteria", "constraints", "decisions", "completed",
                "relative_files", "failed_approaches", "open_tasks", "next_step")}
            for entry in self.case.get("checkpoint_reply", []):
                payload[entry["section"]].append({
                    "text": entry["text"], "trust": entry["trust"],
                    "source_ids": [self.aliases[identifier] for identifier in entry["source_ids"]]})
            yield ProviderEvent("text_delta", text=json.dumps(payload, ensure_ascii=False))
        else:
            yield ProviderEvent("text_delta", text="Offline capture; no answer quality score.")


async def _seed(repo, directory, case):
    project = repo.create_project(case["id"], "coding")
    conversation = repo.create_conversation("project_work", "Target", project_id=project["id"])
    aliases = {}
    for raw in case["conversation"]:
        row = repo._insert_message(conversation["id"], raw["role"], raw["content"])
        aliases[raw["id"]] = row["id"]
    foreign_ids = []
    for index, identifier in enumerate(case["forbidden_source_ids"]):
        other_project = project if index == 0 else repo.create_project("Foreign", "coding")
        other = repo.create_conversation("project_work", "Forbidden work", project_id=other_project["id"])
        row = repo._insert_message(other["id"], "user", f"FOREIGN_SECRET_{identifier}")
        aliases[identifier] = row["id"]
        foreign_ids.append(row["id"])
    attachments = []
    for raw in case.get("attachments", []):
        path = directory / (case["id"] + "-" + raw["id"] + ".txt")
        path.write_text(raw["content"], encoding="utf-8")
        attachments.append(dict(id=case["id"] + "-" + raw["id"], kind="text", name=path.name,
            media_type="text/plain", byte_size=len(raw["content"].encode()),
            sha256=hashlib.sha256(raw["content"].encode()).hexdigest(), storage_key=path.name))
    if attachments:
        old = repo.start_model_run(conversation_id=conversation["id"], content="Attached source files.",
            idempotency_key=case["id"] + "-old", provider_profile_id="builtin-demo", model_id="demo", attachments=attachments)
        aliases["attachment-upload"] = old["user_message_id"]
        repo.transition_model_run(old["id"], {"queued"}, "cancelled")
        references = [dict(attachment_id=case["id"] + "-" + raw["id"],
                           source_start=raw["start"], source_end=raw["end"])
                      for raw in case["attachments"] if raw.get("selected")]
    parameters = dict(conversation_id=conversation["id"],
        content=case["current"]["content"] * case.get("current_repeat", 1),
        idempotency_key=case["id"], provider_profile_id="builtin-demo", model_id="demo")
    if attachments:
        async def sink(event):
            pass
        runtime = ChatRunService(repo, sink)
        with patch("agent.runtime.create_provider", lambda profile: FixtureProvider(case, aliases)):
            result = await runtime.start(dict(parameters, attachment_references=references))
        run = result["run"]
        # start() has persisted the real immutable selection plan. Assembly is
        # driven below, under the selected mutation, with our capture provider.
        task = runtime.run_tasks.pop(run["id"])
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    else:
        run = repo.start_model_run(**parameters)
    aliases[case["current"]["id"]] = run["user_message_id"]
    # Persisted clones, not original attachment IDs, are the request's sources.
    with repo._connect() as connection:
        for attachment in connection.execute("SELECT id,source_attachment_id FROM message_attachments WHERE message_id=?", (run["user_message_id"],)):
            aliases[attachment["source_attachment_id"].removeprefix(case["id"] + "-")] = attachment["id"]
    return run, aliases, foreign_ids


async def _run_case(repo, directory, case, mutation):
    run, aliases, foreign_ids = await _seed(repo, directory, case)
    reverse = {value: key for key, value in aliases.items()}
    service, provider = ContextEngineService(repo), FixtureProvider(case, aliases)
    packed, checkpoint, error = None, None, None
    with apply_mutation(mutation, foreign_ids=foreign_ids):
        try:
            if case.get("compact"):
                await service.compact_once(run, provider, context_window=32768, max_output_tokens=512)
                checkpoint = service.context_repository.active_checkpoint(run["conversation_id"])
            packed = await service.assemble_async(run, context_window=case.get("context_window", 32768), max_output_tokens=512)
            service.persist_snapshot(run["id"], 0, packed)
            async for _event in provider.stream_events("demo", packed.messages, tools=False,
                    options=GenerationOptions(purpose="final_response", max_output_tokens=512)):
                pass
        except (ContextEngineError, CoreDataError, ProviderError) as caught:
            error = caught.code
    status = service.context_repository.compaction_status(run["conversation_id"])
    requests = [call for call in provider.calls if call["purpose"] == "final_response"]
    messages = requests[-1]["messages"] if requests else []
    wire = json.dumps(messages, ensure_ascii=False)
    items = [item.to_record() for item in packed.items if item.included] if packed else []
    source_ids = {reverse.get(item["source_id"], item["source_id"]) for item in items}
    checkpoint_entries = []
    if checkpoint:
        checkpoint_entries = [entry for entries in json.loads(checkpoint["content_json"]).values() for entry in entries]
        source_ids.update(reverse.get(identifier, identifier) for entry in checkpoint_entries for identifier in entry["source_ids"])
    ranges = [{"source_id": reverse.get(item["source_id"], item["source_id"]),
               "start": item["source_start"], "end": item["source_end"]}
              for item in items if item["source_type"] == "attachment"]
    checks = []
    def check(kind, expected, actual, passed):
        checks.append(dict(kind=kind, expected=expected, actual=actual, passed=bool(passed)))
    check("expected_error", case["expected_error"], error, error == case["expected_error"])
    actual_status = status["status"] if status else None
    check("terminal_status", case["expected_terminal_status"], actual_status, actual_status == case["expected_terminal_status"])
    if case.get("provider_error"):
        status_error = status["error_code"] if status else None
        check("terminal_error", case["provider_error"], status_error, status_error == case["provider_error"])
    for identifier in case["expected_source_ids"]:
        check("expected_source", identifier, identifier in source_ids, identifier in source_ids)
    for identifier in case["forbidden_source_ids"]:
        check("forbidden_source", identifier, identifier in source_ids, identifier not in source_ids)
        marker = f"FOREIGN_SECRET_{identifier}"
        check("forbidden_fact", marker, marker in wire, marker not in wire)
    for fact in case["expected_facts"]:
        check("expected_fact", fact, fact in wire, fact in wire)
    for fact in case.get("forbidden_facts", []):
        check("forbidden_fact", fact, fact in wire, fact not in wire)
    for expected in case["source_ranges"]:
        check("source_range", expected, ranges, expected in ranges)
    previews = []
    if packed and case.get("attachments"):
        snapshot = service.context_repository.preview(run["id"])
        for item in snapshot["items"]:
            if item["source_type"] == "attachment" and item["included"]:
                source = service.context_repository.source_preview(run["id"], item["id"])["source"]
                previews.append({"source_id": reverse.get(item["source_id"], item["source_id"]),
                                 "content": source["content"], "start": source["source_start"], "end": source["source_end"]})
        for expected in case["expected_previews"]:
            check("source_preview", expected, previews, expected in previews)
    if case.get("compact") and not case["expected_error"]:
        check("compression_applied", True, bool(packed and packed.compression_applied), packed and packed.compression_applied)
        coverage = {key: checkpoint.get(key) for key in ("start_sequence", "start_offset", "through_sequence", "through_offset")} if checkpoint else None
        check("checkpoint_coverage", case["checkpoint_coverage"], coverage, coverage == case["checkpoint_coverage"])
        for expected in case["checkpoint_reply"]:
            actual = [entry for entry in checkpoint_entries if entry["text"] == expected["text"]]
            wanted = dict(text=expected["text"], trust=expected["trust"], source_ids=[aliases[key] for key in expected["source_ids"]])
            check("checkpoint_provenance", expected, bool(wanted in actual), wanted in actual)
    if case.get("attachments"):
        selected_items = [item for item in items if item["source_type"] == "attachment"]
        check("external_trust", "external_untrusted, never system role", [item["trust"] for item in selected_items],
              bool(selected_items) and all(item["trust"] == "external_untrusted" for item in selected_items)
              and all(fact not in json.dumps([m for m in messages if m["role"] == "system"], ensure_ascii=False)
                      for fact in case["expected_facts"]))
    if not case["expected_error"]:
        check("provider_request", True, bool(requests), bool(requests))
        check("system_policy", "system", messages[0]["role"] if messages else None, messages and messages[0]["role"] == "system")
    if packed:
        check("context_limit", case.get("context_window", 32768), packed.context_window,
              packed.context_window == case.get("context_window", 32768)
              and packed.estimated_tokens + packed.output_reserve < case.get("context_window", 32768))
    check("provider_blocked_on_error", bool(case["expected_error"]), not requests,
          not case["expected_error"] or not requests)
    evidence = dict(source_ids=sorted(str(identifier) for identifier in source_ids if identifier),
        source_ranges=ranges, error=error, terminal_status=actual_status,
        source_previews=previews,
        status_error=status["error_code"] if status else None,
        compression_applied=bool(packed and packed.compression_applied),
        provider_purposes=[call["purpose"] for call in provider.calls])
    if checkpoint:
        evidence["source_ids"] = sorted("checkpoint" if identifier == checkpoint["id"] else identifier for identifier in evidence["source_ids"])
    return dict(id=case["id"], assertions=checks, evidence=evidence)


def run_suite(*, mutation: str | None = None) -> EvalReport:
    if mutation is not None and mutation not in REQUIRED_MUTATIONS:
        raise ValueError(f"Unknown mutation: {mutation}")
    manifest = json.loads((HERE / "cases/manifest.json").read_text(encoding="utf-8"))
    report = EvalReport(suite=manifest["version"])
    async def execute(directory):
        repo = CoreRepository(directory / "synthetic.db")
        for case in manifest["cases"]:
            result = await _run_case(repo, directory, case, mutation)
            report.cases.append(result)
            report.failures.extend(f'{case["id"]}:{check["kind"]}:{check["expected"]}'
                                   for check in result["assertions"] if not check["passed"])
    with TemporaryDirectory(prefix="context-eval-") as temporary:
        # Repository transaction context managers do not close SQLite handles.
        # Track only this synthetic suite's handles for deterministic Windows cleanup.
        connections = []
        owner_thread = threading.get_ident()
        open_database = storage_module._open_database
        def tracked_open(*args, **kwargs):
            connection = open_database(*args, **kwargs)
            if threading.get_ident() == owner_thread:
                connections.append(connection)
            return connection
        with patch.dict(os.environ, {"AGENT_DATA_DIR": temporary}), \
                patch.object(storage_module, "_open_database", tracked_open), \
                patch.object(repository_module, "_open_database", tracked_open):
            try:
                asyncio.run(execute(Path(temporary)))
            finally:
                for connection in connections:
                    connection.close()
                gc.collect()
    if len(report.cases) < 48 or sum(len(case["assertions"]) for case in report.cases) < 240:
        report.failures.append("suite:coverage_floor")
    if len({case["id"] for case in report.cases}) != len(report.cases):
        report.failures.append("suite:duplicate_case_id")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline context reliability gate")
    parser.add_argument("--verify-mutations", action="store_true", help="require all six negative controls to fail behavioral assertions")
    args = parser.parse_args(argv)
    report = run_suite()
    output = asdict(report)
    output["atomic_assertions"] = sum(len(case["assertions"]) for case in report.cases)
    output["passed_assertions"] = sum(check["passed"] for case in report.cases for check in case["assertions"])
    failed = bool(report.failures)
    if args.verify_mutations:
        output["mutations"] = {}
        for name in REQUIRED_MUTATIONS:
            mutant = run_suite(mutation=name)
            killed = bool(mutant.failures)
            output["mutations"][name] = dict(killed=killed, failures=mutant.failures,
                cases=[case for case in mutant.cases if any(not check["passed"] for check in case["assertions"])])
            failed = failed or not killed
    print(json.dumps(output, ensure_ascii=True, indent=2))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
