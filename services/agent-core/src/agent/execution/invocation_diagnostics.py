"""Persist invocation outcome metadata without document bodies or credentials."""
import json


def record(repository, spec, *, finish_reason=None, usage=None, text_characters=0,
           tool_characters=0, phase='model_output', error=None):
    details = getattr(error, 'details', {}) or {}
    text_characters = max(text_characters, details.get('text_character_count', 0))
    tool_characters = max(tool_characters, details.get('tool_argument_character_count', 0))
    value = {
        'max_output_tokens': spec.max_output_tokens, 'phase': phase,
        'text_characters': text_characters, 'tool_argument_characters': tool_characters,
        'partial_text': bool(text_characters and error),
        'partial_tool_arguments': bool(tool_characters and error),
    }
    reason = finish_reason or details.get('finish_reason') or details.get('incomplete_reason')
    if reason:
        value['finish_reason'] = str(reason)[:128]
    if usage:
        value['usage'] = {key: count for key, count in usage.items()
                          if key in {'input_tokens', 'output_tokens', 'cached_input_tokens'}
                          and type(count) is int and count >= 0}
    if error:
        value['error_code'] = getattr(error, 'code', 'internal_error')
    with repository._connect() as connection:
        old = connection.execute('SELECT payload_json FROM invocation_diagnostics WHERE spec_id=?', (spec.spec_id,)).fetchone()
        previous = json.loads(old[0]) if old else {}
        for key in ('text_characters', 'tool_argument_characters'):
            value[key] = max(previous.get(key, 0), value[key])
        for key in ('partial_text', 'partial_tool_arguments'):
            value[key] = previous.get(key, False) or value[key]
        connection.execute('INSERT OR REPLACE INTO invocation_diagnostics VALUES (?,?)',
                           (spec.spec_id, json.dumps({**previous, **value})))


def tool_characters(events):
    return sum(len(json.dumps(event.tool_call.arguments, ensure_ascii=False))
               for event in events if event.tool_call is not None)
