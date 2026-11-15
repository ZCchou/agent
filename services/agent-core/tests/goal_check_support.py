"""Script the separate completion-check turn in lifecycle fixtures."""
import json


def goal_check_content(payload):
    try:
        value = json.loads(payload['messages'][-1]['content'])
    except (KeyError, ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get('phase') != 'goal_verification':
        return None
    execution = value.get('execution_plan', {})
    scopes = execution.get('source_scopes') or [
        {'source_id': item['source_id'], 'operation': item['operation'],
         'scope': 'selected' if item['operation'] == 'read' and item.get('completed_target_ids') else 'full',
         'target_ids': item.get('completed_target_ids', []) if item['operation'] == 'read' else [], 'reason': ''}
        for item in value.get('observed_sources', [])]
    selected = {item['source_id'] for item in scopes}
    scopes = [*scopes, *[
        {'source_id': item['id'], 'operation': 'read', 'scope': 'metadata', 'target_ids': [],
         'reason': 'Original task selects another attachment'}
        for item in value['request'].get('attachments', []) if item['id'] not in selected]]
    goals = None if value.get('requirements') else {
        'source_scopes': scopes,
        'deliverables': [{'path': item['path'], 'format': item['path'].rsplit('.', 1)[-1]}
            for item in execution.get('deliverables', [])],
        'success_criteria': execution.get('success_criteria', [])}
    return json.dumps({'status': 'satisfied', 'summary': 'Fixture completion check',
        'unmet_requirements': [], 'questions': [], 'goal_requirements': goals,
        'evidence_required': value.get('evidence_generation') is not None})
