"""Semantic tool-route verification using synthetic observations only."""
from __future__ import annotations

import json
import secrets
from typing import Any

from agent.execution.execution_wait import current_activity
from agent.provider_tool_adapter import parse_structured_output, structured_messages
from agent.providers import GenerationOptions, ProviderError, closing_provider_stream
from agent.storage import CoreDataError
from agent.tool_contracts import validate_tool_call


PROBE_VERSION = 2
MAX_FORMAT_ATTEMPTS = 3
_FORMAT_ERRORS = {'model_tool_protocol_failed', 'invalid_tool_call'}


async def probe_route(provider, model_id: str, mode: str) -> dict[str, Any]:
    """Return serializable route evidence, propagating execution/transport failures.

    Only explicit capability rejection and exhausted protocol retries establish
    unsupported format. Each stage makes at most three provider calls. No real
    filesystem operation is invoked and no overall generation timeout is imposed.
    """
    if mode not in {'native', 'structured'}:
        raise ValueError('Probe mode must be native or structured.')
    result = {
        'mode': mode, 'usable': False, 'status': 'unsupported',
        'format_ok': False, 'observation_ok': False, 'scope_ok': False,
        'forced_final_ok': False, 'voluntary_stop': False,
        'attempts': {'request': 0, 'observation': 0, 'forced_final': 0},
        'actual_calls': 0, 'error': None,
    }

    async def generate(messages, stage, *, enabled):
        for attempt in range(MAX_FORMAT_ATTEMPTS):
            result['attempts'][stage] += 1
            result['actual_calls'] += 1
            text_parts, calls, reasoning_parts = [], [], []
            structured = mode == 'structured' and enabled
            outbound = structured_messages(messages, {'fs.list'}) if structured else messages
            try:
                async with closing_provider_stream(provider.stream_events(
                    model_id, outbound, tools=enabled and mode == 'native',
                    options=GenerationOptions(
                        purpose='tool_review', max_output_tokens=512,
                        allowed_tools=('fs.list',) if enabled else (),
                    ),
                )) as stream:
                    async for event in stream:
                        if event.kind == 'text_delta' and event.text:
                            text_parts.append(event.text)
                        if event.kind == 'tool_call' and event.tool_call is not None:
                            calls.append(validate_tool_call(event.tool_call))
                        if event.opaque_reasoning_delta:
                            reasoning_parts.append(event.opaque_reasoning_delta)
                        activity = current_activity.get()
                        if activity is not None and event.kind not in {'provider_diagnostic', 'usage'}:
                            activity.progress(stage='tool_verification', probe_route=mode,
                                              probe_stage=stage, characters=len(event.text or ''))
                content = ''.join(text_parts)
                if structured:
                    if calls:
                        raise ProviderError('model_tool_protocol_failed', 'Expected a structured envelope.')
                    content, call = parse_structured_output(content)
                    calls = [call] if call is not None else []
                if stage == 'request':
                    if len(calls) != 1 or calls[0].name != 'fs.list' \
                            or calls[0].arguments.get('path') != '.':
                        raise ProviderError('model_tool_protocol_failed', 'Expected one fs.list call for dot.')
                elif not calls:
                    try:
                        answer = json.loads(content or '')
                    except (ValueError, TypeError) as error:
                        raise ProviderError('model_tool_protocol_failed', 'Expected a JSON final answer.') from error
                    if not isinstance(answer, dict) or set(answer) != {'nonce', 'identifiers'} \
                            or not isinstance(answer['nonce'], str) \
                            or not isinstance(answer['identifiers'], list) \
                            or not all(isinstance(value, str) for value in answer['identifiers']):
                        raise ProviderError('model_tool_protocol_failed', 'Invalid final answer schema.')
                return {'content': content or '', 'calls': calls,
                        'reasoning': ''.join(reasoning_parts)}
            except (ProviderError, CoreDataError) as error:
                if error.code == 'unsupported_capability':
                    result['error'] = {'code': error.code, 'stage': stage, 'message': str(error)}
                    return None
                if error.code not in _FORMAT_ERRORS:
                    raise
                result['error'] = {'code': error.code, 'stage': stage, 'message': str(error)}
                if attempt + 1 == MAX_FORMAT_ATTEMPTS:
                    return None
        return None

    messages = [{'role': 'user', 'content': (
        'This is a synthetic tool verification. Call fs.list exactly once with path ".". '
        'A synthetic observation will follow. Then return only a JSON object with nonce '
        'copied from that observation and identifiers containing exactly the first two '
        'entry names in their original order. Do not inspect or request other entries.'
    )}]
    requested = await generate(messages, 'request', enabled=True)
    if requested is None:
        return result
    result['format_ok'] = True
    result['error'] = None
    call = requested['calls'][0]
    nonce = secrets.token_hex(12)
    names = [f'{secrets.token_hex(8)}.txt' for _ in range(3)]
    observation = json.dumps({'nonce': nonce, 'entries': [{'name': name} for name in names]})
    if mode == 'native':
        assistant = {
            'role': 'assistant', 'content': requested['content'] or None,
            'tool_calls': [{'id': call.provider_call_id, 'type': 'function', 'function': {
                'name': call.name, 'arguments': json.dumps(call.arguments),
            }}],
        }
        if requested['reasoning']:
            assistant['reasoning_content'] = requested['reasoning']
        messages.extend([assistant, {'role': 'tool', 'tool_call_id': call.provider_call_id,
                                    'content': observation}])
    else:
        messages.extend([
            {'role': 'assistant', 'content': json.dumps({
                'type': 'tool_call', 'id': call.provider_call_id,
                'name': call.name, 'arguments': call.arguments,
            })},
            {'role': 'user', 'content': 'Synthetic fs.list result:' + observation},
        ])
    voluntary = await generate(messages, 'observation', enabled=True)
    result['voluntary_stop'] = voluntary is not None and not voluntary['calls']
    # Both finalization attempts use the same observed call/result transcript.
    # A voluntary extra call is recorded, never executed or accepted as an answer.
    forced_messages = [*messages, {'role': 'user', 'content': (
        'Tool use is now disabled. Finish from the synthetic observation already supplied. '
        'Return the required JSON object with its nonce and exactly the first two entry names. '
        'Do not make another tool call.'
    )}]
    forced = await generate(forced_messages, 'forced_final', enabled=False)
    if forced is not None and not forced['calls']:
        answer = json.loads(forced['content'])
        result['observation_ok'] = answer['nonce'] == nonce and all(
            name in names for name in answer['identifiers']) and bool(answer['identifiers'])
        result['scope_ok'] = answer['identifiers'] == names[:2]
        result['forced_final_ok'] = result['observation_ok'] and result['scope_ok']
    result['usable'] = all(result[field] for field in (
        'format_ok', 'observation_ok', 'scope_ok', 'forced_final_ok'))
    if result['usable']:
        result['status'] = 'verified'
        result['error'] = None
    elif result['error'] is None:
        result['error'] = {'code': 'tool_probe_semantic_failed', 'stage': 'forced_final',
                           'message': 'The final answer did not preserve the observed identifiers and scope.'}
    return result
