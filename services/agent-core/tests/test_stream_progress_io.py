from types import SimpleNamespace
from agent.execution.activity import _StreamProgressWrites


def test_first_finish_and_repetition_transitions_immediate_without_losing_counts():
    now=[0.]
    events=[]
    writes=_StreamProgressWrites(lambda:now[0])
    activity=SimpleNamespace(progress=lambda **event:events.append(event))
    def record(kind,count,repeated=False):
        writes.record(activity,dict(kind=kind,characters=count,reasoning_characters=0,repetitive_output=repeated))
    record('text_delta',2)
    assert len(events)==1
    for _ in range(10):record('text_delta',3)
    assert len(events)==1
    now[0]=0.201
    record('text_delta',4)
    assert events[-1]['characters']==34
    record('text_delta',1,True)
    assert events[-1]['repetitive_output']
    record('text_delta',2,True)
    record('finish',0)
    assert events[-1]['kind']=='finish'
    assert sum(event['characters'] for event in events)==39
