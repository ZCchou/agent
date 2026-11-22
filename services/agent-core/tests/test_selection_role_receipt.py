import asyncio,json
import pytest
from agent.execution.activity import associate
from agent.storage import CoreDataError
from test_prepared_review_dispatch import prepared_fixture
from agent.execution import document_review_selection as selection
from agent.execution import document_goal_runtime as new

@pytest.mark.parametrize('bind_gap',[False,True])
def test_uncertain_scope_requires_exact_gap_digest_in_published_report_artifact(tmp_path,monkeypatch,bind_gap):
 async def exercise():
  service,run,attempt,root,current,body,invoked,published,_=await prepared_fixture(tmp_path,monkeypatch)
  original=service._review_targets_tool
  async def with_gap(*args):
   value=await original(*args);result=json.loads(value)
   gap={'target_id':'p-1','reason':'范围待确认','text_sha256':selection.sha('Example content')}
   with service.repository._connect() as db:
    payload=json.loads(db.execute('SELECT payload_json FROM task_goal_execution WHERE task_id=?',(root['id'],)).fetchone()[0]);payload['_review_selection']={'scope_uncertainties':[gap]}
    db.execute('UPDATE task_goal_execution SET payload_json=? WHERE task_id=?',(json.dumps(payload),root['id']))
    if bind_gap:
     manifest=service.documents.get(run['id'],result['artifact_id'])['manifest'];manifest['review_report']['scope_gap_sha256']=selection.scope_gap_sha256([gap])
     db.execute('UPDATE document_artifacts SET manifest_json=? WHERE id=?',(json.dumps(manifest),result['artifact_id']))
   return value
  service._review_targets_tool=with_gap
  try:
   if bind_gap:
    with associate(service.execution_monitor,root['id']):assert await new.dispatch_explicit_review(service,run,attempt['id'],current,body,())
    assert published[-1]['has_scope_gaps'] and published[-1]['scope_gap_sha256']
   else:
    with pytest.raises(CoreDataError) as caught:
     with associate(service.execution_monitor,root['id']):await new.dispatch_explicit_review(service,run,attempt['id'],current,body,())
    assert caught.value.code=='document_review_invalid' and not published
  finally:await service.shutdown()
 asyncio.run(exercise())


def test_old_prepared_receipt_cannot_bypass_new_scope_role_policy(tmp_path,monkeypatch):
 async def exercise():
  service,run,attempt,root,current,body,invoked,published,_=await prepared_fixture(tmp_path,monkeypatch)
  current_policy=selection.review_selection_policy
  try:
   monkeypatch.setattr(selection,'review_selection_policy',lambda:'legacy-policy')
   with associate(service.execution_monitor,root['id']):assert await new.dispatch_explicit_review(service,run,attempt['id'],current,body,())
   assert len(invoked)==1
   monkeypatch.setattr(selection,'review_selection_policy',current_policy)
   with associate(service.execution_monitor,root['id']):assert await new.dispatch_explicit_review(service,run,attempt['id'],current,body,())
   assert len(invoked)==2
   with associate(service.execution_monitor,root['id']):assert await new.dispatch_explicit_review(service,run,attempt['id'],current,body,())
   assert len(invoked)==2
  finally:await service.shutdown()
 asyncio.run(exercise())
