"""The offline gate must fail when context behavior is deliberately broken."""
import importlib.util
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "evals/context/run_context_eval.py"


def evaluator():
    spec = importlib.util.spec_from_file_location("context_eval_sentinels", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_gate_runs_real_cases_and_reports_independent_evidence():
    module = evaluator()
    assert callable(getattr(module, "run_suite", None)), "missing repository-backed suite API"
    report = module.run_suite()
    assert report.failures == []
    assert len(report.cases) >= 48
    assert sum(len(case["assertions"]) for case in report.cases) >= 240
    cases = {case["id"]: case for case in report.cases}
    assert cases["sentinel-attachment"]["evidence"]["source_ranges"] == [
        {"source_id": "selected", "start": 7, "end": 22}]
    assert cases["sentinel-recovery"]["evidence"]["terminal_status"] == "failed"
    assert cases["sentinel-compressed"]["evidence"]["compression_applied"] is True
    assert asdict(report) == asdict(module.run_suite()), "generated IDs must not change the report"


@pytest.mark.parametrize("mutation,assertion", [
    ("drop_history", "expected_fact"),
    ("inject_foreign_source", "forbidden_source"),
    ("upgrade_external_trust", "external_trust"),
    ("drop_selected_attachment", "source_range"),
    ("ignore_context_limit", "expected_error"),
    ("leave_compaction_running", "terminal_status"),
])
def test_each_mutant_is_killed_by_behavior(mutation, assertion):
    module = evaluator()
    assert callable(getattr(module, "run_suite", None)), "missing mutation execution"
    report = module.run_suite(mutation=mutation)
    assert report.failures
    assert any(not check["passed"] and check["kind"] == assertion
               for case in report.cases for check in case["assertions"])
    # Patches must not contaminate a later unmutated invocation.
    assert module.run_suite().failures == []


def test_cli_rejects_unknown_flags_instead_of_silently_passing():
    result = subprocess.run([sys.executable, str(SCRIPT), "--not-a-real-option"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_cli_executes_and_reports_every_required_mutation():
    result = subprocess.run([sys.executable, str(SCRIPT), "--verify-mutations"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert set(report["mutations"]) == {
        "drop_history", "inject_foreign_source", "upgrade_external_trust",
        "drop_selected_attachment", "ignore_context_limit", "leave_compaction_running"}
    assert all(item["killed"] and item["failures"] for item in report["mutations"].values())
