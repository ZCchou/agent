import copy

import pytest

from agent.execution.document_review_selection import preclassified_example_tables, _valid_base_batches
from agent.execution.selection_roles import source_role_observations


def table_fields():
    def field(key, text, kind="paragraph"):
        return dict(id=key, text=text, kind=kind, field_type="text",
            extraction_status="text_extracted", chapter_path=["设备"], position={"source": key})
    return [field("p-0", "示例：设备配置如表1"), field("p-1", "表1 设备配置"),
            field("t-0-r0-c0", "设备", "table_cell"), field("t-0-r1-c0", "型号ABC123", "table_cell")]


def exclude(fields, request="审阅正式内容，跳过空白、占位和示例。"):
    return preclassified_example_tables(fields, request, source_role_observations(fields))


def test_readable_uniquely_referenced_example_table_is_preclassified_and_source_bound():
    fields = table_fields()
    decisions = exclude(fields)
    assert set(decisions) == {"t-0-r0-c0", "t-0-r1-c0"}
    assert all(value["context_ids"] == ["p-0"] and value["basis_code"] == "E:example" for value in decisions.values())
    state = dict(decisions=decisions, base_decisions=copy.deepcopy(decisions), batches=[])
    assert _valid_base_batches(state, fields, {"sha256": "source"}, None, preexcluded=decisions)
    assert not _valid_base_batches(state, fields, {"sha256": "source"}, None)
    forged = copy.deepcopy(state)
    forged["base_decisions"]["t-0-r1-c0"]["text_sha256"] = "forged"
    assert not _valid_base_batches(forged, fields, {"sha256": "source"}, None, preexcluded=decisions)


@pytest.mark.parametrize("change", ["formal", "unreadable", "location", "chapter", "caption"])
def test_conflicting_unreadable_or_unlocated_tables_keep_semantic_selection(change):
    fields = table_fields()
    if change == "formal":
        fields[-1]["text"] = "本设备正式采用ABC123"
    elif change == "unreadable":
        fields[-1]["extraction_status"] = "requires_ocr_or_visual_review"
    elif change == "location":
        fields[-1]["position"] = {}
    elif change == "chapter":
        fields[-1]["chapter_path"] = ["另一章节"]
    else:
        fields[1]["text"] = "表2 设备配置"
    assert not exclude(fields)


@pytest.mark.parametrize("user_request", ["审阅所有内容，包括示例。", "不要跳过示例，只审阅表格。"])
def test_user_must_explicitly_request_excluding_examples(user_request):
    assert not exclude(table_fields(), user_request)
