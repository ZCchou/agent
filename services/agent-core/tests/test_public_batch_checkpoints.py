import asyncio
import json

from agent.document_review import _batch_references
from agent.execution.public_review_draft import public_review_draft
from test_prepared_review_dispatch import prepared_fixture


def test_citation_must_match_an_option_presented_in_this_batch():
    source = {'id':'ev-a','title':'Standard F','clause_number':'4.5.1','locator':{'page':17},
        'quote_options':[{'quote_id':'q-a','text':'The actual source words.'}]}
    refs = _batch_references('[ev-a|q-a] [ev-a|q-other] [ev-b|q-a]', [source])
    assert refs == [{'id':'ev-a','quote_id':'q-a','quote':'The actual source words.',
                     'title':'Standard F','clause':'4.5.1','page':17}]


def test_cancel_keeps_completed_public_batch_without_a_final_artifact(tmp_path,monkeypatch):
    async def exercise():
        service,run,attempt,task,current,*_ = await prepared_fixture(tmp_path,monkeypatch)
        try:
            goals=service.task_plans.goals.get(task['id'])
            selected={**goals['source_scopes'][0],'target_ids':['p-0']}
            selected.pop('selector',None)
            requirements={key:goals[key] for key in ('deliverables','success_criteria')}
            requirements['source_scopes']=[selected]
            goals=service.task_plans.capture_requirements(task['id'],requirements,
                expected_revision=goals['revision'],reconciled=True)
            with service.repository._connect() as db:
                db.execute('UPDATE task_goal_execution SET goal_revision=?,request_digest=? WHERE task_id=?',
                    (goals['revision'],goals['request_digest'],task['id']))
                template=db.execute("SELECT id,sha256,project_id FROM document_artifacts WHERE run_id=? AND kind='template'",(run['id'],)).fetchone()
                identity={'project_generation':0,'targets':['p-0'],'template_id':template[0],
                          'template_sha256':template[1],'project_id':template[2]}
                section={'body':'saved batch prose','target_ids':['p-0'],'goal_revision':goals['revision'],
                         'request_digest':goals['request_digest']}
                db.execute('INSERT INTO execution_wait_checkpoints VALUES (?,?,?,?,?,?,?,?)',
                    (task['id'],'document_review','document-public-review-test',json.dumps(identity),
                     json.dumps(section),1,0,0))
            service.execution_monitor.finish_task(task['id'],'cancelled',error_code='cancelled_by_user')
            body=public_review_draft(service,run)
            assert 'saved batch prose' in body and '本地文件交付尚未核验' in body
            service.rag_grounding.runs[run['id']]['generation']=1
            assert 'saved batch prose' not in public_review_draft(service,run)
        finally:
            await service.shutdown()
    asyncio.run(exercise())
