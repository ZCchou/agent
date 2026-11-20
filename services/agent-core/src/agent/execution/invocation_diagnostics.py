"""Persist invocation outcome metadata without document bodies or credentials."""
import errno
import json

import httpcore
import httpx


_TRANSPORT_TYPES = {
    getattr(module, name): f'{module.__name__}.{name}'
    for module in (httpx, httpcore)
    for name in ('NetworkError', 'ConnectError', 'ReadError', 'WriteError', 'CloseError')
    if hasattr(module, name)
}
_SOCKET_CODES = {
    getattr(errno, name) for name in (
        'ECONNRESET', 'ECONNABORTED', 'ECONNREFUSED', 'ENETRESET', 'ENETDOWN',
        'ENETUNREACH', 'EHOSTDOWN', 'EHOSTUNREACH', 'ENOTCONN', 'ESHUTDOWN',
        'EPIPE', 'ETIMEDOUT', 'EBADF', 'EADDRNOTAVAIL', 'ENOBUFS',
    ) if hasattr(errno, name)
}
# Winsock values may appear as errno even when winerror is unavailable.
_WINDOWS_SOCKET_CODES = {64, 109, 121, 995, 1225, 1236, 10049, 10050, 10051,
                         10052, 10053, 10054, 10055, 10057, 10058, 10060, 10061,
                         10064, 10065}


def _network_fault(error):
    fault = {}
    seen = set()
    while error is not None and id(error) not in seen and len(seen) < 16:
        seen.add(id(error))
        label = _TRANSPORT_TYPES.get(type(error))
        if label and label not in fault.get('types', []):
            fault.setdefault('types', []).append(label)
        if isinstance(error, OSError):
            for key, allowed in (('errno', _SOCKET_CODES | _WINDOWS_SOCKET_CODES),
                                 ('winerror', _WINDOWS_SOCKET_CODES)):
                code = getattr(error, key, None)
                if type(code) is int and code in allowed:
                    fault[key] = code
        error = error.__cause__ or error.__context__
    return fault


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
        if value['error_code'] == 'connection_reset':
            fault = _network_fault(error)
            if fault:
                value['network_error'] = fault
    if error and getattr(error, 'code', None) == 'model_tool_protocol_failed':
        from agent.native_tool_diagnostics import sanitize, BLOCKS
        protocol = sanitize(details.get('native_tool_diagnostic'))
        eligible = details.get('native_tool_correction_eligible')
        if type(eligible) is bool:
            protocol['correction_eligible'] = eligible
        blocked = details.get('native_tool_runtime_blocked_reason')
        if isinstance(blocked, str) and blocked in BLOCKS:
            protocol['runtime_blocked_reason'] = blocked
        if protocol:
            value['native_tool_protocol'] = protocol
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
