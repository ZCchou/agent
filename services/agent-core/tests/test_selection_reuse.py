import asyncio
import json

from agent.document_review import _hash
from agent.execution import document_review_selection as selector
from agent.execution.execution_wait import ExecutionMonitor
from agent.execution.source_inventory import SourceInventory
from agent.execution.task_plans import TaskPlans
from agent.storage import CoreRepository
from test_document_review_selection import CONDITION, fields, reply


def test_selection_cache_preserves_scope_and_rechecks_uncertainties(tmp_path):
    core = CoreRepository(tmp_path / 'core.db')
    project = core.create_project('Review', 'work')['id']
    other_project = core.create_project('Different project', 'work')['id']
    with core._connect() as db:
        db.executemany('INSERT OR REPLACE INTO rag_projects VALUES(?,2)', [(project,), (other_project,)])
    monitor = ExecutionMonitor(core)
    plans = TaskPlans(core)
    inventory = fields()
    digest = selector.sha(inventory)
    environment = dict(project_id=project, project_generation=2, model_id='model', provider_id='managed:model',
                       managed_model={'artifact_digest':'weights','template_digest':'chat-template'}, reasoning_effort='low',
                       sources={'index_ids':['idx'],'library_ids':['lib'],'version_ids':['ver']},
                       generation_config={'route_config_fingerprint':'route'})
    model = {'provider_id':'managed:model','model_id':'model','context_window':8192,
             'generation_config':environment['generation_config']}

    def start(n, project_id=project, request=CONDITION, extra=False):
        chat = core.create_conversation('project_work','Review',project_id=project_id)
        run = core.start_model_run(conversation_id=chat['id'], content=request, idempotency_key=n,
                                   provider_profile_id='managed:model', model_id='model')
        root = monitor.start_task(run_id=run['id'], project_id=project_id)
        source = 'source-' + n
        template = {'id':'artifact-' + n,'sha256':digest,'manifest':{'authorized_source_id':source}}
        plans.create(root['id'], {'content':request,'attachments':[{'id':source,'sha256':digest}]})
        SourceInventory(core).register(root['id'], source, template['id'], digest, inventory, complete=True)
        goals = plans.capture_requirements(root['id'], {
            'source_scopes':[{'source_id':source,'operation':'review','scope':'selected','target_ids':[],
                              'selector':{'kind':'selection_pending','condition':request},'reason':request}],
            'deliverables':[{'path':'review.md','format':'markdown'}],
            'success_criteria':['Extra requirement' if extra else request]}, expected_revision=0)
        with core._connect() as db:
            db.execute('INSERT INTO task_goal_execution VALUES(?,?,?,?)',
                       (root['id'],goals['revision'],goals['request_digest'],'{}'))
        return root, template, request

    def run(task, context, *, uncertain=False, model_identity=model):
        root, template, request = task
        calls = []
        async def classify(payload, schema, correction):
            calls.append((payload.get('selection_phase','base'), [f['id'] for f in payload['fields']]))
            value = reply(payload)
            if uncertain:
                for i, f in enumerate(payload['fields']):
                    if f['id'] == 'p-3':
                        value['decisions'][i] = 'U:ambiguous'
            return value
        selected = asyncio.run(selector.resolve_review_selection(core,root['id'],template,request,inventory,
            generate=classify, fits=lambda *_:True, fence=(root['revision'],root['attempt']),
            model_identity=model_identity, reuse_environment=context))
        with core._connect() as db:
            state = json.loads(db.execute('SELECT payload_json FROM task_goal_execution WHERE task_id=?',
                                          (root['id'],)).fetchone()[0])['_review_selection']
        return selected, calls, state

    prior = start('prior')
    _,_,old = run(prior,environment,uncertain=True)
    assert {x['target_id'] for x in old['scope_uncertainties']} == {'p-3'}
    root, template, _ = prior
    old['identity']['role_review_contract_sha256'] = 'older-role-contract'
    with core._connect() as db:
        db.execute('UPDATE task_goal_execution SET payload_json=? WHERE task_id=?',
                   (json.dumps({'_review_selection':old,'_prepared_review':{'body':'private old report'}}),root['id']))
        sealed = dict(environment, template_id=template['id'],template_sha256=digest,request_sha256=_hash(CONDITION),
                      authorization_source_id=template['manifest']['authorized_source_id'])
        db.execute('INSERT INTO execution_wait_checkpoints VALUES(?,?,?,?,?,?,?,?)',
                   (root['id'],'document_review','document-public-review-prior',json.dumps(sealed),
                    json.dumps({'body':'private old report'}),1,1,1.0))
    monitor.finish_task(root['id'])
    current = start('new')
    selected, calls, state = run(current,environment)
    assert state['reused_selection']['from_task_id'] == root['id']
    assert state['identity']['source_id'] == current[1]['manifest']['authorized_source_id']
    assert selected == ['p-1','p-3'] and not state['scope_uncertainties']
    assert 'private old report' not in json.dumps(state)
    assert [ids for phase,ids in calls if phase == 'base'] == [['p-3']]
    assert set(state['decisions']) == {f['id'] for f in inventory}

    # A saved seed cannot bypass its original model/index identity on a later resume.
    _, resumed, rebuilt = run(current, dict(environment, sources={'index_ids':['replaced']}))
    assert 'reused_selection' not in rebuilt
    assert any('p-1' in ids for _, ids in resumed)


    scenarios = [('tenant',start('tenant',other_project),dict(environment,project_id=other_project),model),
                 ('generation',start('generation'),dict(environment,project_generation=3),model),
                 ('indexes',start('indexes'),dict(environment,sources={'index_ids':['new']}),model),
                 ('weights',start('weights'),dict(environment,managed_model={'artifact_digest':'new'}),model),
                 ('route',start('route'),environment,dict(model,context_window=16128)),
                 ('condition',start('condition',request=CONDITION+' Also review the equipment.'),environment,model),
                 ('constraint',start('constraint',extra=True),environment,model)]
    for label, task, context, identity in scenarios:
        _, calls, state = run(task,context,model_identity=identity)
        assert 'reused_selection' not in state, label
        assert any('p-1' in ids for _,ids in calls), label

    with core._connect() as db:
        old['base_decisions']['p-1']['decision'] = 'exclude'
        db.execute('UPDATE task_goal_execution SET payload_json=? WHERE task_id=?',
                   (json.dumps({'_review_selection':old}),root['id']))
    _, _, state = run(start('damaged-receipt'),environment)
    assert 'reused_selection' not in state
