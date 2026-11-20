import asyncio
import pytest
from agent.execution import execution_wait as wait
from agent.execution.activity import associate,monitored_events
from agent.providers import ProviderEvent
from test_execution_wait import foundation

@pytest.mark.parametrize('busy',[True,None])
def test_duplicate_progress_and_cpu_unknown_pause_after_no_effective_progress(foundation,busy):
 _,monitor,clock=foundation;root=monitor.start_task(run_id='bounded',policy={'no_progress_probe_seconds':60});diagnoses=[]
 async def work(activity):
  while True:
   activity.progress(completed=0,total=100);activity.heartbeat();activity.resource(active=busy)
   await asyncio.sleep(0)
 async def scenario():
  task=asyncio.create_task(monitor.execute_stage(root['id'],'worker',work,diagnose=lambda a:diagnoses.append(1) or wait.Diagnostic(activity_observed=busy)))
  try:
   while monitor.get_task(root['id'])['status']!='waiting_recovery' and clock.now<1000300:await asyncio.sleep(0)
   assert monitor.get_task(root['id'])['status']=='waiting_recovery'
   assert diagnoses
  finally:
   task.cancel();await asyncio.gather(task,return_exceptions=True)
 asyncio.run(scenario())


def test_real_stream_after_public_output_still_diagnoses_but_never_replays(foundation):
 _,monitor,clock=foundation;root=monitor.start_task(run_id='stream',policy={'no_progress_probe_seconds':60});calls=[];diagnoses=[];text=[]
 async def factory():
  calls.append(1);yield ProviderEvent('text_delta',text='Public saved body')
  while True:await asyncio.sleep(0)
 async def consume():
  with associate(monitor,root['id']):
   async for item in monitored_events(factory,safe_to_retry=True,diagnose=lambda a:diagnoses.append(1) or wait.Diagnostic(process_exited=True)):
    text.append(item.text)
 async def scenario():
  task=asyncio.create_task(consume())
  try:
   while monitor.get_task(root['id'])['status']!='waiting_recovery' and clock.now<1000300:await asyncio.sleep(0)
   assert monitor.get_task(root['id'])['status']=='waiting_recovery'
   assert diagnoses and calls==[1] and text==['Public saved body']
  finally:
   task.cancel();await asyncio.gather(task,return_exceptions=True)
 asyncio.run(scenario())



def test_nested_real_stream_reports_real_characters_and_runs_beyond_no_progress_window(foundation):
 _,monitor,clock=foundation;root=monitor.start_task(run_id='nested',policy={'no_progress_probe_seconds':60});text=[]
 async def factory():
  for _ in range(100):
   clock.now+=5
   yield ProviderEvent('text_delta',text='a')
   await asyncio.sleep(0)
 async def work(activity):
  async for event in monitored_events(factory):text.append(event.text)
  assert activity.replay_unsafe
  return len(text)
 with associate(monitor,root['id']):assert asyncio.run(monitor.execute_stage(root['id'],'business',work))==100
 assert clock.now>1000500 and monitor.get_task(root['id'])['status']=='running'



def test_digest_only_repetition_observer_crosses_event_boundaries_and_allows_normal_repetition():
 from agent.execution.output_progress import OutputProgress
 repeated=OutputProgress();body='A repeated public sentence. '
 states=[repeated.feed(c) for c in body*500]
 assert states[0] and not states[-1]
 assert body not in str(repeated.__dict__)
 normal=OutputProgress()
 for i in range(300):
  assert normal.feed(f'Section {i} has its own actual value {i*i}. '+body*2)
 assert len(normal.counts)<=256 and len(normal.blocks)<=256
 assert repeated.feed('New distinct actual information with a substantive changed value 54321.')


def test_actual_repetitive_stream_can_pause_without_losing_public_draft_or_replaying(foundation):
 _,monitor,clock=foundation;root=monitor.start_task(run_id='repeat',policy={'no_progress_probe_seconds':60});received=[];calls=[]
 async def factory():
  calls.append(1)
  while True:
   yield ProviderEvent('text_delta',text='A repeated public sentence. '*16)
   await asyncio.sleep(0)
 async def consume():
  with associate(monitor,root['id']):
   async for event in monitored_events(factory,safe_to_retry=True):received.append(event.text)
 async def scenario():
  task=asyncio.create_task(consume())
  try:
   while monitor.get_task(root['id'])['status']!='waiting_recovery' and clock.now<1001000:await asyncio.sleep(0)
   assert monitor.get_task(root['id'])['status']=='waiting_recovery'
   assert received and calls==[1] and monitor.get_task(root['id'])['can_continue']
  finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
 asyncio.run(scenario())



def test_identical_checkpoint_does_not_reset_no_progress_or_recovery_budget(foundation):
 core,monitor,clock=foundation;root=monitor.start_task(run_id='checkpoint');activity=monitor._begin(root['id'],'worker')
 assert activity.checkpoint('saved',{'source':'same'},{'result':'same'})
 previous=activity.progress_clock
 with core._connect() as db:db.execute('UPDATE execution_wait_tasks SET auto_recoveries=1 WHERE id=?',(root['id'],))
 clock.now+=10
 assert activity.checkpoint('saved',{'source':'same'},{'result':'same'})
 assert activity.progress_clock==previous and monitor.get_task(root['id'])['auto_recoveries']==1
