import asyncio
import pytest
from agent.execution.activity import associate,run_stage
from agent.execution import document_goal_runtime as candidate
from agent.storage import CoreDataError
from test_prepared_review_dispatch import prepared_fixture

@pytest.mark.parametrize('cancel',[False,True])
def test_prepared_review_owns_nested_stage_fence_until_publication(tmp_path,monkeypatch,cancel):
 async def exercise():
  service,run,attempt,root,current,body,invoked,published,_=await prepared_fixture(tmp_path,monkeypatch)
  original=service._review_targets_tool
  async def monitored_review(*args):
   async def operation(activity):
    assert activity is not None
    result=await original(*args)
    if cancel:
     latest=service.execution_monitor.get_task(root['id'])
     service.execution_monitor.control(root['id'],'cancel',revision=latest['revision'],idempotency_key='cancel-review-stage')
    return result
   return await run_stage('document_review',operation)
  service._review_targets_tool=monitored_review
  try:
   with associate(service.execution_monitor,root['id']):
    if cancel:
     with pytest.raises((CoreDataError,asyncio.CancelledError)):
      await candidate.dispatch_explicit_review(service,run,attempt['id'],current,body,())
     assert not published
    else:
     assert await candidate.dispatch_explicit_review(service,run,attempt['id'],current,body,())=='Published controlled report'
     assert len(invoked)==len(published)==1
  finally:await service.shutdown()
 asyncio.run(exercise())


def test_recorded_evidence_gaps_do_not_block_publishing_verified_draft(tmp_path,monkeypatch):
 async def exercise():
  service,run,attempt,root,current,body,invoked,published,_=await prepared_fixture(tmp_path,monkeypatch)
  original=service._review_targets_tool
  async def gap_review(*args):
   value=await original(*args)
   with service.repository._connect() as db:
    db.execute("UPDATE task_review_proofs SET outcome='gap' WHERE task_id=?",(root['id'],))
   return value
  service._review_targets_tool=gap_review
  try:
   with associate(service.execution_monitor,root['id']):
    assert await candidate.dispatch_explicit_review(service,run,attempt['id'],current,body,())=='Published controlled report'
   assert len(published)==1
  finally:await service.shutdown()
 asyncio.run(exercise())


def test_active_stage_retry_resumes_prepared_review_without_replaying_inspection(tmp_path,monkeypatch):
 async def exercise():
  service,run,attempt,root,current,body,invoked,published,_=await prepared_fixture(tmp_path,monkeypatch)
  monitor=service.execution_monitor
  monitor.sleep=lambda _:asyncio.sleep(.02)
  original=service._review_targets_tool
  entered=asyncio.Event()
  async def operation(*args):
   from agent.execution.activity import current_activity
   a=current_activity.get()
   identity={'source':current['attachments'][0]['sha256']}
   if not entered.is_set():
    assert a.checkpoint('confirmed-selection',identity,{'p-0':'include'})
    entered.set()
    await asyncio.Future()
   assert a.load_checkpoint('confirmed-selection',identity)=={'p-0':'include'}
   return await original(*args)
  service._review_targets_tool=operation
  try:
   with associate(monitor,root['id']):
    job=asyncio.create_task(candidate.dispatch_explicit_review(service,run,attempt['id'],current,body,()))
    await asyncio.wait_for(entered.wait(),5)
    fresh=monitor.get_task(root['id'])
    monitor.control(root['id'],'retry',revision=fresh['revision'],idempotency_key='stage-retry')
    assert await asyncio.wait_for(job,5)=='Published controlled report'
   ops=service.tool_loop.repository.list_for_run(run['id'])
   assert [op['status'] for op in ops if op['tool_name']=='document.review_targets']==['interrupted','completed']
   assert len([op for op in ops if op['tool_name']=='document.inspect_template'])==len(published)==1
  finally:await service.shutdown()
 asyncio.run(exercise())
