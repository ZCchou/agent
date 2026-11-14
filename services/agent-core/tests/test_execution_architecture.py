from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).parents[1] / "src" / "agent"


def imported_modules(relative_path: str) -> set[str]:
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_pure_execution_modules_do_not_depend_on_io_or_runtime() -> None:
    forbidden = {"sqlite3", "agent.providers", "agent.runtime"}
    for module in (
        "requirements.py", "web_intent.py", "compatibility.py", "selection.py",
        "planner.py", "routes.py", "output_budget.py",
    ):
        imports = imported_modules(f"execution/{module}")
        assert not (imports & forbidden), f"{module} crossed the pure planning boundary"


def test_execution_repository_does_not_depend_on_runtime_protocol_or_provider() -> None:
    imports = imported_modules("repositories/execution_repository.py")
    assert "agent.runtime" not in imports
    assert "agent.protocol" not in imports
    assert "agent.providers" not in imports


def test_execution_planner_does_not_own_web_search_routing() -> None:
    imports = imported_modules("execution/planner.py")
    assert not any(module.startswith("agent.web_search") for module in imports)


def test_web_search_domain_service_does_not_depend_on_storage_or_settings() -> None:
    imports = imported_modules("web_search/service.py")
    assert "agent.storage" not in imports
    assert "sqlite3" not in imports
    assert not any(module.startswith("agent.settings") for module in imports)


def test_runtime_does_not_select_vendor_adapter_routes() -> None:
    source = (ROOT / "runtime.py").read_text(encoding="utf-8").casefold()
    for route_literal in (
        "deepseek.anthropic_web", "deepseek.responses", "kimi.openai_chat",
        "openai.responses", "anthropic.messages", "ollama.chat",
    ):
        assert route_literal not in source


def test_execution_contract_matches_core_domain() -> None:
    contract_path = Path(__file__).parents[3] / "contracts" / "unified-invocation-v3.4.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract["ipc_version"] == 24
    assert contract["core_schema_version"] == 31
    assert contract["settings_schema_version"] == 19
    assert contract["core_version"] == "0.16.0-preview.4"
    assert contract["build_id"] == "schema31-agent-unified-v1"
    assert set(contract["capability_keys"]) == {
        "text_generation", "vision_input", "reasoning", "function_calling",
        "web_search", "structured_web_evidence", "file_read", "file_write",
    }
    assert set(contract["invocation_kinds"]) == {"model", "host_search"}
    assert set(contract["adapter_protocols"]) == set(contract["adapters"])
    assert set(contract["adapter_protocols"].values()) <= set(contract["protocols"])
