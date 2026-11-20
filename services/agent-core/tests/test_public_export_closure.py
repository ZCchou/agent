import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from agent.runtime import ChatRunService
from agent.storage import CoreDataError


@pytest.mark.parametrize("body", ["", "The complete public review, with a detailed finding.",
    "The public report.\n\n[打开文件](<result.md>)\n\nMissing source"])
@pytest.mark.parametrize("invalid_file", [False, True])
def test_export_closure_preserves_public_body_and_independently_checks_file(body, invalid_file):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE document_artifacts (id, run_id, output_path, manifest_json, status)")
    connection.execute("INSERT INTO document_artifacts VALUES (?,?,?,?,?)",
        ("artifact", "run", "result.md", json.dumps({"missing": ["Missing source"],
         "review_issues": ["Needs review"], "version_issues": ["Version gap"]}), "published"))
    checked = []
    def verify(run_id, artifact_id):
        checked.append((run_id, artifact_id))
        if invalid_file:
            raise CoreDataError("document_publish_failed", "File changed")
    service = object.__new__(ChatRunService)
    service.repository = SimpleNamespace(_connect=lambda: connection)
    service.rag_grounding = SimpleNamespace(validate=lambda run_id: checked.append(run_id))
    service.documents = SimpleNamespace(verify_publication=verify)
    run = {"id": "run", "rag_decision": {"task_type": "document_export"}}
    call = service._finalize_rag_answer(run, "attempt", None, [], body, None, None, 1)
    if invalid_file:
        with pytest.raises(CoreDataError, match="File changed"):
            asyncio.run(call)
    else:
        result = asyncio.run(call)
        assert result.startswith(body or "文档已保存")
        assert "result.md" in result
        assert all(text in result for text in ("Missing source", "Needs review", "Version gap"))
        if body:
            assert result.count(body) == 1
        assert result.count("[打开文件](<result.md>)") == 1
        assert result.count("Missing source") == 1
    assert checked == ["run", ("run", "artifact")]


def test_model_text_cannot_finish_a_requested_review_publication():
    service = object.__new__(ChatRunService)
    run = {"id": "review", "_requires_review_publication": True}
    body = "<tool_call>task.update_plan</tool_call>"
    with pytest.raises(CoreDataError) as caught:
        asyncio.run(service._finish_task_goal(run, "attempt", body, None, None, None, None, [], (), None, False))
    assert caught.value.code == "document_publish_unverified"
