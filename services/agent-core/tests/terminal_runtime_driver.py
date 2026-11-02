"""Offline model/Core half of the real Host terminal integration test (JSON lines)."""
import asyncio
import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch
import httpx
from agent.runtime import ChatRunService
from agent.storage import CoreRepository

async def main():
    root, kind = Path(sys.argv[1]), sys.argv[2]
    requests = []
    real_client = httpx.AsyncClient
    def respond(req):
        payload = json.loads(req.content)
        requests.append(payload)
        names = {item['function']['name'] for item in payload.get('tools', [])}
        if kind == 'standard_chat':
            assert 'terminal_run' not in names
            delta, reason = {'content': 'No terminal in standard chat.'}, 'stop'
        elif len(requests) == 1:
            assert 'terminal_run' in names
            delta, reason = {'tool_calls': [{'index': 0, 'id': 'run-check', 'type': 'function',
                'function': {'name': 'terminal_run', 'arguments': json.dumps({'command': '& ./test.ps1'})}}]}, 'tool_calls'
        else:
            results = [m['content'] for m in payload['messages'] if m['role'] == 'tool']
            assert results, payload
            assert 'test passed' in results[-1] and '7' in results[-1]
            delta, reason = {'content': 'Verification failed with exit code 7; stdout test passed was not sufficient.'}, 'stop'
        return httpx.Response(200, text='data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':reason}]})+'\n\ndata: [DONE]\n\n')
    async def no_advisor(*args, **kwargs):
        return None, 'synthetic_advisor_unavailable'
    repository = CoreRepository(root / (kind+'.db'))
    if kind == 'project_work':
        project = repository.create_project('Synthetic', 'coding')
        work = repository.create_conversation(kind, 'Verify', project_id=project['id'])
    else:
        work = repository.create_conversation(kind, 'Chat', category='casual')
    repository.update_conversation_routing(work['id'], 'automatic', 'balanced', 'synthetic', 'model')
    feedback_count = 0
    async def sink(event):
        nonlocal feedback_count
        if event['event_type'] == 'run.invocation_completed':
            # Mirror the Host's public reinjection after a successful invocation.
            feedback = copy.deepcopy(configuration)
            signature = event['data']['capability_signature']
            feedback['model_route_health'] = {'model': [dict(route_id='chat',
                capability_signature=signature, health_status='ready', consecutive_failures=0)]}
            for state in feedback['model_route_capabilities']['model']:
                if state['capability_signature'] == signature:
                    state.update(status='verified', verified_at='2026-10-23T12:00:00Z')
            service.configure_provider(feedback)
            feedback_count += 1
        if event['event_type'] == 'run.tool_requested':
            operation = event['data']['operation']
            print(json.dumps({'operation':operation}), flush=True)
            result = json.loads(await asyncio.to_thread(sys.stdin.readline))
            service.tool_status(operation['id'], 'executing')
            service.resolve_tool(operation['id'], True, json.dumps(result))
    service = ChatRunService(repository, sink)
    configuration = {'id':'synthetic','name':'Synthetic','kind':'openai_compatible',
        'base_url':'https://synthetic.invalid','api_key':'synthetic','allowed_model_ids':['model'],
        'automatic_model_ids':['model'],'model_tool_modes':{'model':'native'},
        'model_capabilities':{'model':{'text':'yes','tools':'yes','context_length':65536}},
        'invocation_routes':[{'route_id':'chat','route_role':'primary','protocol':'openai_chat',
            'adapter_id':'deepseek.openai_chat','manifest_version':'synthetic-v1','config_fingerprint':'synthetic','source_provenance':'none'}],
        'model_route_capabilities':{'model':[
            {'route_id':'chat','capability_signature':['text'],'status':'verified','route_config_fingerprint':'synthetic'},
            {'route_id':'chat','capability_signature':['function_tools','text'],'status':'declared','route_config_fingerprint':'synthetic'}]}}
    service.configure_provider(configuration)
    with patch.object(httpx,'AsyncClient',lambda **kw:real_client(transport=httpx.MockTransport(respond),**kw)), patch.object(ChatRunService,'_preflight_intent_advisor',no_advisor):
        started=await service.start({'conversation_id':work['id'],'content':'Run the project test and report the real result.' if kind=='project_work' else 'Hello',
            'idempotency_key':'verify','selection':{'mode':'automatic','provider_profile_id':'synthetic','model_id':'model','routing_policy':{'preset':'balanced'}}})
        await asyncio.wait_for(service.tasks[started['run']['id']],30)
    run=repository.get_model_run(started['run']['id'])
    assert run['status']=='completed', run
    operations=service.list_tool_operations(run['id'])
    assert len(operations)==(1 if kind=='project_work' else 0)
    if operations:
        assert json.loads(operations[0]['result_summary'])['exit_code']==7
    assert feedback_count == (2 if kind == 'project_work' else 1)
    print(json.dumps({'done':True,'kind':kind,'operation_count':len(operations),'host_feedback_count':feedback_count}),flush=True)

if __name__=='__main__':
    asyncio.run(main())
