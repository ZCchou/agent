import asyncio

import pytest

from agent.execution.activity import associate
from agent.execution.document_goal_runtime import dispatch_explicit_review
from agent.execution.public_review_draft import public_review_draft
from agent.storage import CoreDataError
from test_prepared_review_dispatch import prepared_fixture


@pytest.mark.parametrize("fault", ["publish_denied", "cancelled", "stale_generation", "corrupt_report"])
def test_terminal_review_preserves_only_verified_content(tmp_path, monkeypatch, fault):
    async def exercise():
        service, run, attempt, root, current, body, reviewed, _, _ = await prepared_fixture(tmp_path, monkeypatch)
        from agent.execution import prepared_review_publication
        receipts = []

        async def denied(_, __, ___, ____, _____, receipt):
            receipts.append(receipt)
            raise CoreDataError("document_publish_failed", "写入未获批准。")

        monkeypatch.setattr(prepared_review_publication, "publish_requested_review", denied)
        try:
            with associate(service.execution_monitor, root['id']), pytest.raises(CoreDataError, match="批准"):
                await dispatch_explicit_review(service, run, attempt['id'], current, body, ())
            if fault == "cancelled":
                service.execution_monitor.finish_task(root['id'], "cancelled", error_code="cancelled_by_user")
            if fault == "stale_generation":
                def stale(_):
                    raise CoreDataError("rag_evidence_stale", "资料已变更。")
                monkeypatch.setattr(service.rag_grounding, "validate", stale)
            if fault == "corrupt_report":
                service.documents.path_for(run["id"], receipts[0]["artifact_id"]).write_text("unverified body")
            text = public_review_draft(service, run)
            if fault in {"stale_generation", "corrupt_report"}:
                assert not text
            else:
                assert text.count("Controlled review report") == 1
                assert "本地文件交付尚未核验" in text and "打开文件" not in text
                assert public_review_draft(service, run) == text
            assert len(reviewed) == len(receipts) == 1
        finally:
            await service.shutdown()
    asyncio.run(exercise())