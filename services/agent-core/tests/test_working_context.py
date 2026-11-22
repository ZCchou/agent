import json

from agent.execution.working_context import compact_tool_history, ProgressGuard


def pair(index, body):
    return [{'role': 'assistant', 'tool_calls': [{'id': str(index), 'type': 'function',
        'function': {'name': 'document.read_attachment', 'arguments': '{"attachment_id":"a"}'}}]},
        {'role': 'tool', 'tool_call_id': str(index), 'content': body}]


def test_repeated_compaction_preserves_requirements_and_tool_pairs():
    messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': '审阅全部内容，保留型号甲'}]
    for index in range(15):
        messages.extend(pair(index, json.dumps({'result_id': str(index), 'preview': '正文' * 3000})))
        compact_tool_history(messages, input_budget=8192, measure=lambda m: len(json.dumps(m)) // 2)
    assert next(m for m in messages if m['role'] == 'user')['content'] == '审阅全部内容，保留型号甲'
    assert len(json.dumps(messages)) < 14000
    calls = {c['id'] for m in messages for c in m.get('tool_calls', [])}
    assert all(m['tool_call_id'] in calls for m in messages if m['role'] == 'tool')


def test_progress_guard_detects_repeat_and_resets_on_changed_result():
    guard = ProgressGuard()
    assert guard.observe('read', {'cursor': 0}, 'same') is None
    assert guard.observe('read', {'cursor': 0}, 'same') is None
    assert guard.observe('read', {'cursor': 0}, 'same') == 'adjust'
    for _ in range(2):
        assert guard.observe('read', {'cursor': 0}, 'same') is None
    assert guard.observe('read', {'cursor': 0}, 'same') == 'stalled'
    assert guard.observe('read', {'cursor': 1}, 'new') is None


def test_alternating_old_ranges_are_not_new_progress():
    guard = ProgressGuard()
    for _ in range(5):
        guard.observe('read', {'cursor': 0}, 'page0')
        guard.observe('read', {'cursor': 1}, 'page1')
    assert guard.observe('read', {'cursor': 0}, 'page0') == 'stalled'
    assert not guard.new_progress
