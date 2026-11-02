import pytest

from agent.storage import CoreDataError
from agent.tool_contracts import ToolCall, validate_tool_call


@pytest.mark.parametrize("cursor", [1, True, [], {}, "x" * 257])
def test_search_rejects_nonopaque_or_unbounded_cursor(cursor):
    call = ToolCall("search-1", "fs.search", {
        "path": ".", "query": "synthetic", "mode": "name", "cursor": cursor,
    })
    with pytest.raises(CoreDataError) as error:
        validate_tool_call(call)
    assert error.value.code == "invalid_tool_call"


@pytest.mark.parametrize("cursor", [None, "snapshot-token", "x" * 256])
def test_search_preserves_host_cursor_without_interpreting_or_expanding_it(cursor):
    call = ToolCall("search-1", "fs.search", {
        "path": ".", "query": "synthetic", "mode": "content", "cursor": cursor,
    })
    assert validate_tool_call(call).arguments["cursor"] == cursor
