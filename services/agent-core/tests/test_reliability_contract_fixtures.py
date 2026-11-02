"""The same wire examples are consumed by Host and desktop boundary tests."""
import asyncio
import json
from pathlib import Path

import pytest

from agent.context_engine.attachment_references import text_range, validate_references
from agent.protocol import PROTOCOL_VERSION, handle_async_message
from agent.storage import CoreDataError
from agent.tool_contracts import search_completeness


FIXTURE = json.loads((Path(__file__).parents[3] / "contracts" / "reliability-repair-fixtures.json").read_text(encoding="utf-8"))


def test_shared_identity_and_reference_fields_cross_core_protocol():
    class Service:
        update_prepared_id = None
        update_admissions = 0
        async def resume_compaction(self, conversation_id, target=None):
            assert conversation_id == "conversation-fixture"
            assert target == FIXTURE["compaction_target"]
            return {"status": "queued", "target": target}

        async def start(self, params):
            references = validate_references(params["attachment_references"])
            assert references == FIXTURE["attachment_references"]
            selected, start, end = text_range(references[0], FIXTURE["range_example"]["text"])
            assert (selected, start, end) == ("目标片段", 4, 8)
            return {"status": "queued", "attachment_references": references}

    async def scenario():
        for kind, params, result_key, expected in [
            ("context.compaction.resume", {"conversation_id": "conversation-fixture", "target": FIXTURE["compaction_target"]},
             "compaction", {"status": "queued", "target": FIXTURE["compaction_target"]}),
            ("model.run.start", {"content": "", "attachment_references": FIXTURE["attachment_references"]},
             "attachment_references", FIXTURE["attachment_references"]),
        ]:
            response = await handle_async_message(json.loads(json.dumps({
                "v": PROTOCOL_VERSION, "id": kind, "type": kind, "params": params,
            })), Service())
            assert response["id"] == kind
            assert response["result"][result_key] == expected
    asyncio.run(scenario())


def test_shared_reference_fixture_cannot_gain_path_authority():
    reference = {**FIXTURE["attachment_references"][0], "storage_key": "outside-authority.txt"}
    with pytest.raises(CoreDataError) as failure:
        validate_references([reference])
    assert failure.value.code == "attachment_reference_invalid"


def test_shared_search_results_keep_completeness_but_not_content_in_audit():
    incomplete, complete = map(search_completeness, FIXTURE["search_pages"])
    assert incomplete == {"complete": False, "truncated": True, "stop_reason": "files", "inspected_files": 10000}
    assert complete == {"complete": True, "truncated": False, "stop_reason": None, "inspected_files": 2}
    assert search_completeness({"matches": []}) is None
