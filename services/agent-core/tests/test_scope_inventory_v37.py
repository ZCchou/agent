import pytest
from agent.storage import CoreDataError
from test_task_plans import service


def fields():
    return [{'id':f'p-{n}','text':text,'original_content':text,'field_type':'content'}
            for n,text in enumerate(['title',' ','one','two','three','four','six'])]


def test_inspection_inventory_is_not_a_review_obligation_and_resolves_prefix(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core,_,root,plans=service(tmp_path)
    store=SourceInventory(core)
    store.register(root['id'],'source','template','hash',fields(),complete=True)
    store.record_read(root['id'],'template','hash',fields()[:6])
    assert plans.completion(root['id'])['ready'] is False
    assert plans.completion(root['id'])['total']==0
    selected=store.resolve(root['id'],{'source_id':'source','operation':'read','scope':'selected',
        'target_ids':[],'selector':{'kind':'prefix_nonempty','count':5},'reason':''})
    assert selected['target_ids']==['p-0','p-2','p-3','p-4','p-5']
    proof=store.coverage(root['id'],selected)
    assert proof['ready'] and proof['completed']==5
    assert store.coverage(root['id'],{**selected,'operation':'review'})['completed']==0


def test_aliases_share_inventory_but_changed_source_does_not_reuse_proof(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core,_,root,_=service(tmp_path);store=SourceInventory(core)
    store.register(root['id'],'source','template','hash',fields(),complete=True)
    store.record_read(root['id'],'template','hash',fields())
    store.register(root['id'],'template','template','newhash',fields(),complete=True)
    scope={'source_id':'source','operation':'read','scope':'selected','target_ids':['p-0'],'reason':''}
    assert not store.coverage(root['id'],scope)['ready']


def test_partial_inventory_does_not_claim_full_or_total_and_truncated_text_is_not_read(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core,_,root,_=service(tmp_path);store=SourceInventory(core)
    store.register(root['id'],'source','template','hash',fields(),complete=False)
    store.record_read(root['id'],'source','hash',[{'id':'p-0','text':'t'}])
    assert not store.coverage(root['id'],{'source_id':'source','operation':'read','scope':'full','target_ids':[],'reason':''})['ready']
    assert not store.coverage(root['id'],{'source_id':'source','operation':'read','scope':'selected','target_ids':['p-0'],'reason':''})['ready']

def test_short_prefix_reports_missing_positions_instead_of_success(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core, _, root, _ = service(tmp_path)
    store = SourceInventory(core)
    store.register(root['id'], 'source', 'template', 'hash', fields()[:2], complete=True)
    store.record_read(root['id'], 'source', 'hash', fields()[:2])
    proof = store.coverage(root['id'], {'source_id': 'source', 'operation': 'read',
        'scope': 'selected', 'target_ids': [], 'selector': {'kind': 'prefix_nonempty', 'count': 5}, 'reason': ''})
    assert not proof['ready']
    assert proof['reason'] == 'selected_positions_missing'
    assert proof['remaining'] == 4


def test_same_source_identity_cannot_replace_content_or_downgrade_complete(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core, _, root, _ = service(tmp_path)
    store = SourceInventory(core)
    store.register(root['id'], 'source', 'template', 'hash', fields(), complete=True)
    store.register(root['id'], 'source', 'template', 'hash', fields()[:2], complete=False)
    scope = {'source_id': 'source', 'operation': 'read', 'scope': 'full', 'target_ids': [], 'reason': ''}
    assert store.resolve(root['id'], scope)['inventory_complete']
    changed = [{**fields()[0], 'original_content': 'replacement'}]
    with pytest.raises(CoreDataError) as failure:
        store.register(root['id'], 'source', 'template', 'hash', changed, complete=False)
    assert failure.value.code == 'task_inventory_conflict'


def test_unreadable_objects_are_required_and_cannot_be_read_proofs(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core, _, root, _ = service(tmp_path)
    store = SourceInventory(core)
    unreadable = [{'id': 'drawing', 'field_type': 'unreadable', 'text': ''}]
    store.register(root['id'], 'source', 'template', 'hash', unreadable, complete=True)
    store.record_read(root['id'], 'source', 'hash', unreadable)
    proof = store.coverage(root['id'], {'source_id': 'source', 'operation': 'read',
        'scope': 'full', 'target_ids': [], 'reason': ''})
    assert not proof['ready'] and proof['remaining'] == 1


def test_scope_preserves_operation_and_requested_metadata():
    from agent.execution.goal_requirements import covers_scope
    required = {'source_id': 'source', 'operation': 'read', 'scope': 'selected',
        'target_ids': ['p-0'], 'reason': '', 'metadata_keys': ['total']}
    assert not covers_scope({**required, 'operation': 'review'}, required)
    assert not covers_scope({**required, 'metadata_keys': []}, required)

def test_distinct_authorized_sources_do_not_merge_by_hash(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core, _, root, _ = service(tmp_path)
    store = SourceInventory(core)
    store.register(root['id'], 'source', 'template', 'hash', fields(), complete=True)
    store.record_read(root['id'], 'source', 'hash', fields())
    store.register(root['id'], 'other-source', 'other-template', 'hash', fields(), complete=True)
    assert not store.coverage(root['id'], {'source_id': 'other-source', 'operation': 'read',
        'scope': 'selected', 'target_ids': ['p-0'], 'reason': ''})['ready']


def test_source_alias_cannot_be_redirected_to_another_inventory(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core, _, root, _ = service(tmp_path)
    store = SourceInventory(core)
    store.register(root['id'], 'source', 'template', 'hash', fields(), complete=True)
    store.register(root['id'], 'other-source', 'other-template', 'hash', fields(), complete=True)
    store.register(root['id'], 'source', 'other-template', 'hash', fields(), complete=True)
    assert store.canonical(root['id'], 'source') == 'source'
    assert store.canonical(root['id'], 'other-source') == 'other-source'
    with pytest.raises(CoreDataError):
        store.canonical(root['id'], 'other-template')


def test_new_artifact_alias_for_same_authorized_source_keeps_read_proofs(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core, _, root, _ = service(tmp_path)
    store = SourceInventory(core)
    store.register(root['id'], 'source', 'template', 'hash', fields(), complete=True)
    store.record_read(root['id'], 'source', 'hash', fields())
    store.register(root['id'], 'source', 'new-template', 'hash', fields(), complete=True)
    assert store.canonical(root['id'], 'new-template') == 'source'

def test_metadata_goal_needs_actual_source_inventory(tmp_path):
    _, _, root, plans = service(tmp_path)
    plans.capture_requirements(root['id'], {'source_scopes': [{'source_id': 'source',
        'operation': 'read', 'scope': 'metadata', 'target_ids': [], 'reason': '',
        'metadata_keys': ['total']}], 'deliverables': [], 'success_criteria': ['返回总数']}, expected_revision=0)
    assert not plans.completion(root['id'])['ready']


def test_malformed_metadata_keys_raise_structured_validation_error():
    from agent.execution.task_plans import validate_requirements
    with pytest.raises(CoreDataError):
        validate_requirements([{'source_id': 'source', 'operation': 'read', 'scope': 'metadata',
            'target_ids': [], 'reason': '', 'metadata_keys': [{}]}], [])

def test_alias_scope_revision_preserves_the_same_requirement(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core, _, root, plans = service(tmp_path)
    store = SourceInventory(core)
    store.register(root['id'], 'source', 'template', 'hash', fields(), complete=True)
    goal = {'source_scopes': [{'source_id': 'source', 'operation': 'read', 'scope': 'selected',
        'target_ids': ['p-0'], 'reason': ''}], 'deliverables': [], 'success_criteria': []}
    plans.capture_requirements(root['id'], goal, expected_revision=0)
    goal['source_scopes'][0]['source_id'] = 'template'
    plans.capture_requirements(root['id'], goal, expected_revision=1)


def test_excluded_scope_does_not_satisfy_read_metadata():
    from agent.execution.goal_requirements import covers_scope
    metadata = {'source_id': 'source', 'operation': 'read', 'scope': 'metadata',
        'target_ids': [], 'reason': ''}
    excluded = {**metadata, 'scope': 'excluded', 'reason': 'skip'}
    assert not covers_scope(excluded, metadata)
    assert not covers_scope(metadata, excluded)

def test_attachment_fragments_prove_docx_positions_without_replacing_inventory(tmp_path):
    import hashlib
    import io
    from docx import Document
    from agent.document_inventory import inventory
    from agent.execution.source_inventory import SourceInventory
    document = Document()
    document.add_paragraph('A long paragraph with exact content')
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = 'left'
    table.cell(0, 1).text = 'right'
    stream = io.BytesIO()
    document.save(stream)
    payload = stream.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    core, _, root, _ = service(tmp_path)
    store = SourceInventory(core)
    store.register(root['id'], 'source', 'template', digest, inventory(payload)['fields'], complete=True)
    locator = {'kind': 'docx', 'part': 'body', 'paragraph': 1}
    text = document.paragraphs[0].text
    scope = {'source_id': 'source', 'operation': 'read', 'scope': 'full', 'target_ids': [], 'reason': ''}
    store.record_attachment_read(root['id'], 'source', digest, {'items': [], 'complete': True},
        {'blocks': [{'id': 'fragment-one', 'text': text[8:], 'character_offset': 8, 'locator': locator}]}, docx_payload=payload)
    assert not store.coverage(root['id'], scope)['ready']
    store.record_attachment_read(root['id'], 'source', digest, {'items': [], 'complete': True},
        {'blocks': [{'id': 'fragment-two', 'text': text[:8], 'character_offset': 0, 'locator': locator},
                    {'id': 'table-row', 'text': 'left | right', 'locator': {'kind': 'docx', 'part': 'body', 'table': 1, 'row': 1}}]},
        docx_payload=payload)
    assert store.coverage(root['id'], scope)['ready']
    material = store.material(root['id'], scope)
    assert material['template_id'] == 'template'
    assert [field['id'] for field in material['fields']] == ['p-0', 't-0-r0-c0', 't-0-r0-c1']


def test_attachment_fragment_mismatch_never_proves_docx_read(tmp_path):
    import hashlib
    import io
    from docx import Document
    from agent.execution.source_inventory import SourceInventory
    document = Document()
    document.add_paragraph('original')
    stream = io.BytesIO()
    document.save(stream)
    payload = stream.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    core, _, root, _ = service(tmp_path)
    store = SourceInventory(core)
    store.record_attachment_read(root['id'], 'source', digest, {'items': [], 'complete': True},
        {'blocks': [{'id': 'fake', 'text': 'changed', 'locator': {'kind': 'docx', 'paragraph': 1}}]}, docx_payload=payload)
    assert not store.coverage(root['id'], {'source_id': 'source', 'operation': 'read',
        'scope': 'full', 'target_ids': [], 'reason': ''})['ready']

def test_identical_template_artifact_keeps_authorized_attachments_distinct(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core, _, root, _ = service(tmp_path, attachments=[{'id': 'source'}, {'id': 'second'}])
    store = SourceInventory(core)
    store.register(root['id'], 'source', 'shared-template', 'hash', fields(), complete=True)
    store.record_read(root['id'], 'source', 'hash', fields())
    store.register(root['id'], 'second', 'shared-template', 'hash', fields(), complete=True)
    assert store.canonical(root['id'], 'second') == 'second'
    assert not store.coverage(root['id'], {'source_id': 'second', 'operation': 'read',
        'scope': 'full', 'target_ids': [], 'reason': ''})['ready']
    with pytest.raises(CoreDataError) as failure:
        store.record_read(root['id'], 'shared-template', 'hash', fields())
    assert failure.value.code == 'task_source_alias_ambiguous'


def test_read_results_cannot_become_review_proofs(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core, _, root, plans = service(tmp_path)
    store = SourceInventory(core)
    store.register(root['id'], 'source', 'template', 'hash', fields(), complete=True)
    plans.register_source(root['id'], 'source', 'template', 'hash', fields(), operation='read')
    plans.record_results(root['id'], 'template', 'hash', [{'target_id': item['id'], 'status': 'reviewed'} for item in fields()])
    scope = {'source_id': 'source', 'operation': 'review', 'scope': 'full', 'target_ids': [], 'reason': ''}
    assert store.coverage(root['id'], scope)['completed'] == 0
    plans.register_source(root['id'], 'source', 'template', 'hash', fields(), operation='review')
    assert store.coverage(root['id'], scope)['completed'] == 0
    plans.record_results(root['id'], 'template', 'hash', [{'target_id': 'p-0', 'status': 'reviewed'}])
    assert store.coverage(root['id'], scope)['completed'] == 1


def test_selection_pending_stays_unresolved_until_model_supplies_positions(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core, _, root, plans = service(tmp_path)
    store = SourceInventory(core)
    store.register(root['id'], 'source', 'template', 'hash', fields(), complete=True)
    scope = {'source_id': 'source', 'operation': 'read', 'scope': 'selected', 'target_ids': [],
        'selector': {'kind': 'selection_pending', 'condition': 'Vibration purpose section'}, 'reason': ''}
    goal = {'source_scopes': [scope], 'deliverables': [], 'success_criteria': ['Extract selected section']}
    plans.capture_requirements(root['id'], goal, expected_revision=0)
    assert not store.coverage(root['id'], scope)['ready']
    assert not plans.completion(root['id'])['ready']
    goal['source_scopes'] = [{**scope, 'target_ids': ['p-3']}]
    goal['source_scopes'][0].pop('selector')
    plans.capture_requirements(root['id'], goal, expected_revision=1)

def test_review_proofs_are_bound_to_attachment_even_with_shared_artifact(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core, _, root, plans = service(tmp_path, attachments=[{'id': 'source'}, {'id': 'second'}])
    store = SourceInventory(core)
    for identity in ('source', 'second'):
        store.register(root['id'], identity, 'shared-template', 'hash', fields(), complete=True)
        plans.register_source(root['id'], identity, 'shared-template', 'hash', fields(), operation='review')
    plans.record_results(root['id'], 'shared-template', 'hash', [{'target_id': 'p-0', 'status': 'reviewed'}],
                         authorization_source_id='source')
    scope = {'source_id': 'source', 'operation': 'review', 'scope': 'selected', 'target_ids': ['p-0'], 'reason': ''}
    assert store.coverage(root['id'], scope)['ready']
    assert not store.coverage(root['id'], {**scope, 'source_id': 'second'})['ready']
    with pytest.raises(CoreDataError):
        plans.record_results(root['id'], 'shared-template', 'hash', [{'target_id': 'p-1', 'status': 'reviewed'}])

def test_actual_legacy_review_payload_migrates_but_synthetic_read_does_not(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    from agent.migrations.v37_scope_closure import migrate
    core, _, root, plans = service(tmp_path)
    store = SourceInventory(core)
    store.register(root['id'], 'source', 'template', 'hash', fields(), complete=True)
    plans.register_source(root['id'], 'source', 'template', 'hash', fields(), operation='review')
    plans.record_results(root['id'], 'template', 'hash', [
        {'target_id': 'p-0', 'status': 'reviewed', 'summary': 'Actual review outcome'},
        {'target_id': 'p-2', 'status': 'reviewed'}])
    with core._connect() as connection:
        connection.execute('DELETE FROM task_review_proofs')
        migrate(connection)
    proof = store.coverage(root['id'], {'source_id': 'source', 'operation': 'review',
        'scope': 'selected', 'target_ids': ['p-0', 'p-2'], 'reason': ''})
    assert proof['completed'] == 1 and proof['remaining'] == 1

def test_grouped_docx_parser_text_proves_only_positions_actually_returned(tmp_path):
    import hashlib
    import io
    from docx import Document
    from agent.execution.source_inventory import SourceInventory
    document = Document()
    for text in ('First', 'Second', 'Third'):
        document.add_paragraph(text)
    stream = io.BytesIO()
    document.save(stream)
    payload = stream.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    core, _, root, _ = service(tmp_path)
    store = SourceInventory(core)
    store.record_attachment_read(root['id'], 'source', digest, {'items': [], 'complete': True},
        {'blocks': [{'id': 'grouped', 'text': 'First\nSecond', 'locator': {'kind': 'docx', 'paragraph': 1}}]},
        docx_payload=payload)
    scope = {'source_id': 'source', 'operation': 'read', 'scope': 'full', 'target_ids': [], 'reason': ''}
    proof = store.coverage(root['id'], scope)
    assert proof['completed'] == 2 and proof['remaining'] == 1
    assert [item['id'] for item in store.material(root['id'], scope)['fields']] == ['p-0', 'p-1']

@pytest.mark.parametrize('with_inventory', [False, True])
def test_review_proofs_require_current_project_evidence_generation(tmp_path, with_inventory):
    from agent.execution.source_inventory import SourceInventory
    core, monitor, root, plans = service(tmp_path)
    project = core.create_project('Proofs', 'work')
    chat = core.create_conversation('project_work', 'Review', project_id=project['id'])
    run = core.start_model_run(content='Review source', conversation_id=chat['id'],
        idempotency_key='proof-generation', provider_profile_id='p', model_id='m')
    monitor.bind_run(root['id'], run['id'])
    with core._connect() as connection:
        connection.execute('INSERT OR REPLACE INTO rag_projects(project_id,generation) VALUES (?,3)', (project['id'],))
    if with_inventory:
        SourceInventory(core).register(root['id'], 'source', 'template', 'hash', fields(), complete=True)
    plans.register_source(root['id'], 'source', 'template', 'hash', fields(), operation='review')
    plans.capture_requirements(root['id'], {'source_scopes': [{'source_id': 'source', 'operation': 'review',
        'scope': 'selected', 'target_ids': ['p-0'], 'reason': ''}], 'deliverables': [], 'success_criteria': []}, expected_revision=0)
    results = [{'target_id': 'p-0', 'status': 'reviewed', 'summary': 'Verified review'}]
    plans.record_results(root['id'], 'template', 'hash', results, evidence_generation=3)
    assert plans.completion(root['id'])['ready']
    with core._connect() as connection:
        connection.execute('UPDATE rag_projects SET generation=4 WHERE project_id=?', (project['id'],))
    assert not plans.completion(root['id'])['ready']
    with core._connect() as connection:
        assert connection.execute('SELECT count(*) FROM task_work_items WHERE result_json IS NOT NULL').fetchone()[0] == 1
    plans.record_results(root['id'], 'template', 'hash', results)
    assert not plans.completion(root['id'])['ready']
    plans.record_results(root['id'], 'template', 'hash', results, evidence_generation=4)
    assert plans.completion(root['id'])['ready']

def test_v37_proof_generation_column_upgrade_is_idempotent(tmp_path):
    from agent.migrations.v37_scope_closure import migrate
    core, _, _, _ = service(tmp_path)
    with core._connect() as connection:
        connection.execute('ALTER TABLE task_review_proofs DROP COLUMN evidence_generation')
        migrate(connection)
        migrate(connection)
        columns = [row[1] for row in connection.execute('PRAGMA table_info(task_review_proofs)')]
    assert columns.count('evidence_generation') == 1

@pytest.mark.parametrize('resolved_ids', [['p-0'], ['p-0', 'p-2', 'p-3']])
def test_pending_selection_preserves_exact_requested_count(tmp_path, resolved_ids):
    core, _, root, plans = service(tmp_path)
    scope = {'source_id': 'source', 'operation': 'read', 'scope': 'selected', 'target_ids': [],
        'selector': {'kind': 'selection_pending', 'condition': 'Equipment parameters', 'count': 2}, 'reason': ''}
    goal = {'source_scopes': [scope], 'deliverables': [], 'success_criteria': []}
    plans.capture_requirements(root['id'], goal, expected_revision=0)
    resolved = {**scope, 'target_ids': resolved_ids}
    resolved.pop('selector')
    with pytest.raises(CoreDataError):
        plans.capture_requirements(root['id'], {**goal, 'source_scopes': [resolved]}, expected_revision=1)
    plans.capture_requirements(root['id'], {**goal, 'source_scopes': [{**resolved, 'target_ids': ['p-0', 'p-2']}]}, expected_revision=1)


def test_completed_historical_review_keeps_unversioned_proof(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core, monitor, root, plans = service(tmp_path)
    project = core.create_project('History', 'work')
    chat = core.create_conversation('project_work', 'History', project_id=project['id'])
    run = core.start_model_run(content='Review source', conversation_id=chat['id'],
        idempotency_key='history-proof', provider_profile_id='p', model_id='m')
    monitor.bind_run(root['id'], run['id'])
    SourceInventory(core).register(root['id'], 'source', 'template', 'hash', fields(), complete=True)
    plans.register_source(root['id'], 'source', 'template', 'hash', fields(), operation='review')
    plans.capture_requirements(root['id'], {'source_scopes': [{'source_id': 'source', 'operation': 'review',
        'scope': 'selected', 'target_ids': ['p-0'], 'reason': ''}], 'deliverables': [], 'success_criteria': []}, expected_revision=0)
    plans.record_results(root['id'], 'template', 'hash', [{'target_id': 'p-0', 'status': 'reviewed'}])
    assert not plans.completion(root['id'])['ready']
    monitor.finish_task(root['id'], 'completed')
    assert plans.completion(root['id'])['ready']

def test_parser_view_change_invalidates_reads_and_rejects_old_view_result(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core, _, root, _ = service(tmp_path)
    store = SourceInventory(core)
    original = [{'id': 'fragment', 'text': 'original'}]
    store.register(root['id'], 'source', 'source', 'hash', original, complete=True, parser_view='parser-v1')
    store.record_read(root['id'], 'source', 'hash', original, parser_view='parser-v1')
    scope = {'source_id': 'source', 'operation': 'read', 'scope': 'full', 'target_ids': [], 'reason': ''}
    assert store.coverage(root['id'], scope)['ready']
    revised = [{'id': 'fragment', 'text': 'reparsed'}]
    store.register(root['id'], 'source', 'source', 'hash', revised, complete=False, parser_view='parser-v2')
    assert not store.coverage(root['id'], scope)['ready']
    with pytest.raises(CoreDataError):
        store.record_read(root['id'], 'source', 'hash', original, parser_view='parser-v1')
    store.register(root['id'], 'source', 'source', 'hash', revised, complete=True, parser_view='parser-v2')
    store.record_read(root['id'], 'source', 'hash', revised, parser_view='parser-v2')
    assert store.coverage(root['id'], scope)['ready']


def test_retained_schema37_database_supports_explicit_qa_migration(tmp_path):
    from agent.storage import CoreRepository
    from agent.migrations.v37_scope_closure import migrate
    core, _, _, _ = service(tmp_path)
    with core._connect() as connection:
        connection.execute('ALTER TABLE task_review_proofs DROP COLUMN evidence_generation')
        connection.execute('ALTER TABLE task_inventory_sources DROP COLUMN parser_view')
        migrate(connection)
    restored = CoreRepository(core.database_path)
    with restored._connect() as connection:
        assert 'evidence_generation' in {row[1] for row in connection.execute('PRAGMA table_info(task_review_proofs)')}
        assert 'parser_view' in {row[1] for row in connection.execute('PRAGMA table_info(task_inventory_sources)')}

def test_parser_view_change_invalidates_review_proof_without_erasing_history(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    core, _, root, plans = service(tmp_path)
    store = SourceInventory(core)
    store.register(root['id'], 'source', 'template', 'hash', fields(), complete=True, parser_view='view-one')
    plans.register_source(root['id'], 'source', 'template', 'hash', fields(), operation='review')
    plans.record_results(root['id'], 'template', 'hash', [{'target_id': 'p-0', 'status': 'reviewed', 'summary': 'Prior review'}])
    scope = {'source_id': 'source', 'operation': 'review', 'scope': 'selected', 'target_ids': ['p-0'], 'reason': ''}
    assert store.coverage(root['id'], scope)['ready']
    revised = [{**item, 'original_content': 'changed'} if item['id'] == 'p-0' else item for item in fields()]
    store.register(root['id'], 'source', 'template', 'hash', revised, complete=True, parser_view='view-two')
    assert not store.coverage(root['id'], scope)['ready']
    with core._connect() as connection:
        assert connection.execute('SELECT count(*) FROM task_work_items WHERE result_json IS NOT NULL').fetchone()[0] == 1
    store.register(root['id'], 'source', 'template', 'hash', fields(), complete=True, parser_view='view-one')
    assert not store.coverage(root['id'], scope)['ready']


def test_legacy_source_hash_rollback_does_not_restore_review_proof(tmp_path):
    _, _, root, plans = service(tmp_path)
    plans.register_source(root['id'], 'source', 'template', 'original-hash', fields(), operation='review')
    plans.record_results(root['id'], 'template', 'original-hash', [{'target_id': 'p-0', 'status': 'reviewed'}])
    plans.capture_requirements(root['id'], {'source_scopes': [{'source_id': 'source', 'operation': 'review',
        'scope': 'selected', 'target_ids': ['p-0'], 'reason': ''}], 'deliverables': [], 'success_criteria': []}, expected_revision=0)
    assert plans.completion(root['id'])['ready']
    plans.register_source(root['id'], 'source', 'template', 'new-hash', fields(), operation='review')
    plans.register_source(root['id'], 'source', 'template', 'original-hash', fields(), operation='review')
    assert not plans.completion(root['id'])['ready']
