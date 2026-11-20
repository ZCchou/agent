"""Resolved edit attachment identities must seed immutable business task scope."""
import asyncio

from agent.runtime import ChatRunService
from test_attachment_references import seed


def test_edit_retained_attachment_can_capture_goal_from_resolved_current_message(tmp_path, monkeypatch):
    repo, original = seed(tmp_path, monkeypatch, bodies=('Actual retained attachment',))

    async def scenario():
        service = ChatRunService(repo, lambda event: asyncio.sleep(0))
        observed = {}

        async def capture_resolved_goal(run_id):
            run = repo.get_model_run(run_id)
            root = service.execution_monitor.start_task(run_id=run_id)
            current = next(message for message in repo.get_run_conversation(run_id)['messages']
                           if message['id'] == run['user_message_id'])
            attachment = current['attachments'][0]
            assert attachment['id'] != 'a0'
            assert attachment['source_attachment_id'] == 'a0'
            assert attachment['sha256'] == repo.get_run_conversation(original['id'])['messages'][0]['attachments'][0]['sha256']
            assert run['conversation_id'] != original['conversation_id']
            observed['run'] = run
            observed['root'] = root
            observed['current'] = current
            observed['digest_before'] = service.task_plans.goals.request_digest(root['id'])
            # This is the same persistent authorization boundary used by
            # document_goal_runtime.prepare after normalize_goal succeeded.
            observed['captured'] = service.task_plans.capture_requirements(root['id'], {
                'source_scopes': [{'source_id': attachment['id'], 'operation': 'review',
                    'scope': 'selected', 'target_ids': [],
                    'selector': {'kind': 'selection_pending', 'condition': 'filled formal content'},
                    'reason': 'Review the retained attachment'}],
                'deliverables': [{'path': 'review.md', 'format': 'markdown'}],
                'success_criteria': ['Review formal content and publish the report']},
                expected_revision=0, fence=(root['revision'], root['attempt']))

        monkeypatch.setattr(service, '_execute', capture_resolved_goal)
        params = dict(conversation_id=original['conversation_id'], edit_message_id=original['user_message_id'],
            content='Review filled formal content in the attachment and save the report.',
            idempotency_key='resolved-edit-goal', provider_profile_id='builtin-demo', model_id='demo',
            attachments=[], attachment_references=[])
        try:
            result = await service.start(params)
            await service.tasks[result['run']['id']]
            assert observed['captured']['source_scopes'][0]['source_id'] == observed['current']['attachments'][0]['id']
            snapshot = service.task_plans.get(observed['root']['id'])['request']
            # Preserve the immutable original request/digest; trusted admission
            # comes from the bound run's sealed execution plan.
            assert snapshot['conversation_id'] == original['conversation_id']
            assert snapshot['content'] == params['content']
            assert snapshot['attachments'] == []
            sealed = service.execution_repository.get_for_run(observed['run']['id'])['resolved_intent']
            assert sealed['effective_attachment_ids'] == [observed['current']['attachments'][0]['id']]
            assert service.task_plans.goals.request_digest(observed['root']['id']) == observed['digest_before']
            assert observed['captured']['request_digest'] == observed['digest_before']
            from agent.storage import CoreDataError
            with repo._connect() as db:
                db.execute("UPDATE task_goal_requirements SET provenance='user_clarification' WHERE task_id=?", (observed['root']['id'],))
            reduced = {key: observed['captured'][key] for key in ('source_scopes', 'deliverables', 'success_criteria')}
            reduced['deliverables'] = []
            with pytest.raises(CoreDataError) as protected:
                service.task_plans.capture_requirements(observed['root']['id'], reduced, expected_revision=1, reconciled=True,
                    fence=(observed['root']['revision'], observed['root']['attempt']))
            assert protected.value.code == 'task_goal_change_requires_input'
            before = snapshot.copy()
            service.task_plans.create(observed['root']['id'], {'content': 'unrelated replacement', 'attachments': []})
            assert service.task_plans.get(observed['root']['id'])['request'] == before
        finally:
            await service.shutdown()

    asyncio.run(scenario())



async def resolved_edit(tmp_path, monkeypatch):
    repo, original = seed(tmp_path, monkeypatch, bodies=('Actual retained attachment', 'Other historical source'))
    service = ChatRunService(repo, lambda event: asyncio.sleep(0))
    async def no_model(run_id):
        pass
    monkeypatch.setattr(service, '_execute', no_model)
    result = await service.start(dict(conversation_id=original['conversation_id'],
        edit_message_id=original['user_message_id'], content='Review retained source',
        idempotency_key='edit-scope-probe', provider_profile_id='builtin-demo', model_id='demo',
        attachments=[], attachment_references=[]))
    await service.tasks[result['run']['id']]
    run = repo.get_model_run(result['run']['id'])
    root = service.execution_monitor.start_task(run_id=run['id'])
    current = next(message for message in repo.get_run_conversation(run['id'])['messages']
                   if message['id'] == run['user_message_id'])
    return repo, service, original, run, root, current


def read_allowed(repo, service, root):
    from agent.context_engine.attachment_references import task_attachment_ids
    with repo._connect() as db:
        return task_attachment_ids(db, root['id'], service.task_plans.get(root['id'])['request'])


def reseal(repo, run, mutate):
    import json
    from agent.execution.domain import stable_hash
    with repo._connect() as db:
        row = db.execute('SELECT resolved_intent_json FROM execution_plans WHERE id=?', (run['execution_plan_id'],)).fetchone()
        intent = json.loads(row[0])
        mutate(intent)
        requirements_hash = stable_hash({'requirements_hash': stable_hash(intent['effective_requirements']),
                                         'attachment_selection': intent['attachment_selection']})
        db.execute('UPDATE execution_plans SET resolved_intent_json=?,resolution_hash=?,requirements_hash=? WHERE id=?',
                   (json.dumps(intent), stable_hash(intent), requirements_hash, run['execution_plan_id']))


def test_resolved_edit_allows_only_sealed_current_materialized_inputs(tmp_path, monkeypatch):
    async def scenario():
        repo, service, original, run, root, current = await resolved_edit(tmp_path, monkeypatch)
        try:
            expected = {attachment['id'] for attachment in current['attachments']}
            assert read_allowed(repo, service, root) == expected
            assert not {'a0', 'a1'} & read_allowed(repo, service, root)
        finally:
            await service.shutdown()
    asyncio.run(scenario())


import pytest


@pytest.mark.parametrize('change', ['source_message', 'current_id', 'source_attachment', 'cross_run',
                                    'resolution_hash', 'requirements_hash', 'selection_hash'])
def test_forged_or_misbound_edit_selection_cannot_authorize_scope(tmp_path, monkeypatch, change):
    from agent.storage import CoreDataError
    async def scenario():
        repo, service, original, run, root, current = await resolved_edit(tmp_path, monkeypatch)
        try:
            if change in {'resolution_hash', 'requirements_hash'}:
                with repo._connect() as db:
                    db.execute('UPDATE execution_plans SET '+change+'=? WHERE id=?', ('tampered', run['execution_plan_id']))
            elif change == 'selection_hash':
                import json
                with repo._connect() as db:
                    value = json.loads(db.execute('SELECT resolved_intent_json FROM execution_plans WHERE id=?', (run['execution_plan_id'],)).fetchone()[0])
                    value['attachment_selection'][0]['source_message_id'] = 'tampered'
                    db.execute('UPDATE execution_plans SET resolved_intent_json=? WHERE id=?', (json.dumps(value), run['execution_plan_id']))
            elif change == 'cross_run':
                with repo._connect() as db:
                    db.execute('UPDATE execution_wait_tasks SET run_id=? WHERE id=?', (original['id'], root['id']))
            else:
                def mutate(value):
                    for item in value['attachment_selection']:
                        if change == 'source_message':
                            item['source_message_id'] = original['user_message_id']
                        elif change == 'source_attachment':
                            item['source_attachment_id'] = 'a0'
                        else:
                            item['current_attachment_id'] = 'a0'
                    if change == 'current_id':
                        value['effective_attachment_ids'] = [item['current_attachment_id'] for item in value['attachment_selection']]
                reseal(repo, run, mutate)
            try:
                allowed = read_allowed(repo, service, root)
            except CoreDataError as error:
                assert error.code == 'attachment_reference_selection_changed'
            else:
                assert change not in {'resolution_hash', 'requirements_hash', 'selection_hash'}, 'A corrupt seal must fail explicitly'
                assert allowed == set()
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_current_upload_and_explicit_partial_history_keep_original_authority(tmp_path, monkeypatch):
    async def scenario():
        repo, original = seed(tmp_path, monkeypatch, bodies=('Selected history body', 'Unselected history'))
        service = ChatRunService(repo, lambda event: asyncio.sleep(0))
        async def no_model(run_id):
            pass
        monkeypatch.setattr(service, '_execute', no_model)
        try:
            uploaded = dict(repo.get_run_conversation(original['id'])['messages'][0]['attachments'][0])
            uploaded['id'] = 'fresh-input'
            for index, extras in enumerate(({'attachments': [uploaded]},
                                           {'attachment_references': [{'attachment_id': 'a0', 'source_start': 0, 'source_end': 8}]},
                                           {})):
                result = await service.start(dict(conversation_id=original['conversation_id'], content='Read selected input',
                    idempotency_key='input-'+str(index), provider_profile_id='builtin-demo', model_id='demo', **extras))
                await service.tasks[result['run']['id']]
                run = repo.get_model_run(result['run']['id'])
                root = service.execution_monitor.start_task(run_id=run['id'])
                current = next(item for item in repo.get_run_conversation(run['id'])['messages'] if item['id'] == run['user_message_id'])
                allowed = read_allowed(repo, service, root)
                assert allowed == {item['id'] for item in current['attachments']}
                assert 'a1' not in allowed
                if index == 1:
                    selected = service.execution_repository.get_for_run(run['id'])['resolved_intent']['attachment_selection'][0]
                    assert selected['source_start'] == 0
                    assert selected['source_end'] == 8
                if index == 2:
                    assert not allowed
                repo.transition_model_run(run['id'], {'queued'}, 'cancelled')
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_cross_project_reference_is_rejected_before_sealed_admission(tmp_path, monkeypatch):
    from agent.storage import CoreDataError
    async def scenario():
        repo, original = seed(tmp_path, monkeypatch, bodies=('Foreign project source',))
        source_project = repo.create_project('Source project', 'research')
        target_project = repo.create_project('Target project', 'research')
        source_chat = repo.create_conversation('project_work', 'Source', project_id=source_project['id'])
        target_chat = repo.create_conversation('project_work', 'Target', project_id=target_project['id'])
        attachment = dict(repo.get_run_conversation(original['id'])['messages'][0]['attachments'][0])
        attachment['id'] = 'foreign-project-input'
        foreign = repo.start_model_run(conversation_id=source_chat['id'], content='Foreign source',
            idempotency_key='foreign-project', provider_profile_id='builtin-demo', model_id='demo', attachments=[attachment])
        repo.transition_model_run(foreign['id'], {'queued'}, 'cancelled')
        service = ChatRunService(repo, lambda event: asyncio.sleep(0))
        try:
            with pytest.raises(CoreDataError) as rejected:
                await service.start(dict(conversation_id=target_chat['id'], content='Read the attachment',
                    idempotency_key='cross-project-reference', provider_profile_id='builtin-demo', model_id='demo',
                    attachment_references=[{'attachment_id': attachment['id']}]))
            assert rejected.value.code == 'attachment_reference_forbidden'
            assert not service.tasks
        finally:
            await service.shutdown()
    asyncio.run(scenario())
