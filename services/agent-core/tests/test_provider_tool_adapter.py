import pytest

from agent.provider_tool_adapter import parse_structured_output
from agent.providers import ProviderError


def test_strict_structured_tool_envelope() -> None:
    text, call = parse_structured_output(
        '{"type":"tool_call","id":"call-1","name":"fs.read","arguments":{"path":"README.md"}}'
    )
    assert text is None
    assert call is not None
    assert call.name == "fs.read"


def test_structured_adapter_does_not_guess_tools_from_prose_or_markdown() -> None:
    with pytest.raises(ProviderError, match="required structured"):
        parse_structured_output('I would call fs.read on README.md')
    with pytest.raises(ProviderError, match="required structured"):
        parse_structured_output('```json\n{"type":"text","content":"hello"}\n```')


def test_structured_text_envelope_has_no_extra_fields() -> None:
    assert parse_structured_output('{"type":"text","content":"done"}') == ("done", None)
    with pytest.raises(ProviderError, match="invalid"):
        parse_structured_output('{"type":"text","content":"done","name":"fs.read"}')
