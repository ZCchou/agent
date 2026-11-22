from agent.document_review import _public_review_page


def test_short_fields_share_capacity_and_long_fields_get_smaller_batches():
    base = dict(stage="review", request="Review", freeform_review=True, token_budget=12000)
    fields = [dict(id=str(i), text="短说明", section_id="s", chapter_path=["温度"], position={}, field_type="text") for i in range(30)]
    short = _public_review_page(base, fields, 0, 0, 4096)
    long = _public_review_page(base, [dict(f, text="温度要求" * 250) for f in fields], 0, 0, 4096)
    assert len(short["fields"]) > 10
    assert 1 <= len(long["fields"]) < len(short["fields"])
    assert short["target_ids"] == [f["id"] for f in fields[:len(short["fields"])]]
