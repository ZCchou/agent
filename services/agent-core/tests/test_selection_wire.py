import copy
import json

from agent.execution.document_review_selection import selection_messages


def test_selection_wire_preserves_field_text_provenance_and_structure():
    fields = [{"text":"示例：a","original_content":"示例：a","style":"斜体"},
              {"text":"a → b","original_content":"a（条件）→ b","source_index":1}]
    payload = {"fields": fields, "source_context_candidates":list(reversed(fields)),
               "source_role_observations":[{"source_index":1}], "request_condition":"跳过示例"}
    prior = copy.deepcopy(payload)
    instruction, user = selection_messages(payload)
    actual = json.loads(user["content"])
    assert "omitted original_content equals the supplied text" in instruction["content"]
    assert "original_content" not in actual["fields"][0]
    assert actual["fields"][1]["original_content"] == fields[1]["original_content"]
    for key in ("fields","source_context_candidates"):
        for item in actual[key]:
            item.setdefault("original_content",item["text"])
    assert actual == prior == payload


def test_letter_output_and_legacy_results_have_the_same_source_proofs():
    from agent.execution.document_review_selection import CODES, WIRE_TO_BASIS, schema_for, validate_decisions

    assert set(WIRE_TO_BASIS.values()) == set(CODES)
    fields = [{"id":str(i),"text":str(i)} for i in range(len(CODES))]
    refs = {"context_refs":[{"index":0,"anchors":[1]}], "context_notes":"", "context_indices":[]}
    offsets = {field["id"]:n for n,field in enumerate(fields)}
    def parse(codes):
        return validate_decisions(dict(refs,decisions=codes), fields, set(offsets), source_indices=offsets)
    assert parse(list(WIRE_TO_BASIS)) == parse(list(WIRE_TO_BASIS.values()))
    assert schema_for(list(offsets.values()),[1],wire=True)["properties"]["decisions"]["items"]["enum"] == list(WIRE_TO_BASIS)
