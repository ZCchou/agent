import asyncio
import json

import pytest

from agent.tool_contracts import ToolCall
from agent.tool_loop import ToolLoopService
from test_tool_repository import started_run


async def perform_search(tmp_path, output):
    repository, tools, run, attempt = started_run(tmp_path)

    async def resolve(event):
        service.resolve(event["data"]["operation"]["id"], success=True, output=output)

    service = ToolLoopService(tools, resolve)
    operation, resolution = await service.request(
        run_id=run["id"], attempt_id=attempt["id"], sequence=1,
        call=ToolCall("search", "fs.search", {"path": ".", "query": "synthetic", "mode": "name"}),
    )
    return operation, tools, resolution, repository


@pytest.mark.parametrize("complete, reason", [(True, None), (False, "bytes"), (False, "deadline")])
def test_search_audit_preserves_completeness_without_paths_or_cursor(tmp_path, complete, reason):
    output = json.dumps({
        "matches": ["synthetic-private-marker.txt"], "inspected_files": 12,
        "next_cursor": "transient-cursor-marker", "complete": complete,
        "truncated": not complete, "stop_reason": reason,
    })
    operation, tools, resolution, repository = asyncio.run(perform_search(tmp_path, output))
    expected = {"complete": complete, "truncated": not complete, "stop_reason": reason, "inspected_files": 12}
    assert operation.get("search_completeness") == expected
    assert tools.get(operation["id"])["search_completeness"] == expected
    assert "synthetic-private-marker" not in operation["result_summary"]
    assert "transient-cursor-marker" not in operation["result_summary"]
    assert resolution.output == output
    message = repository.record_tool_event(
        operation["run_id"], operation["id"], "fs.search", "completed", len(output), None,
        search_summary=operation["search_completeness"],
    )
    assert json.loads(message["content"])["search_completeness"] == expected
    assert "synthetic-private-marker" not in message["content"]
    assert "transient-cursor-marker" not in message["content"]


@pytest.mark.parametrize("output", [
    "old result", '{"matches": []}',
    '{"complete": true, "truncated": true, "stop_reason": "bytes", "inspected_files": 12}',
    '{"complete": true, "truncated": false, "stop_reason": null, "inspected_files": -1}',
])
def test_missing_or_invalid_search_metadata_is_never_recorded_as_complete(tmp_path, output):
    operation, _, _, _ = asyncio.run(perform_search(tmp_path, output))
    assert operation.get("search_completeness") is None
