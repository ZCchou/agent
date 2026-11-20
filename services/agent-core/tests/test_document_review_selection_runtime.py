"""Real runtime method + real compiler/SQLite, mocked audited provider response."""
import ast
import asyncio
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import MethodType, SimpleNamespace
import pytest

from docx import Document

from agent import runtime
from agent.document_inventory import inventory
from agent.documents import DocumentWorkflow
from agent.execution.activity import associate
from agent.execution.compiler import InvocationCompiler
from agent.execution.execution_wait import ExecutionMonitor
from agent.execution.source_inventory import SourceInventory
from agent.execution.task_plans import TaskPlans
from agent.storage import CoreRepository
from test_unified_invocation_v3 import model_spec
from test_document_review_selection import CONDITION, reply

@pytest.mark.parametrize('schema_supported',[True,False])
def test_runtime_omitted_ids_resolves_complete_source_before_review_callback(tmp_path,monkeypatch,schema_supported,source_mismatch=False,renewed_artifact=False):
    async def scenario():
        core=CoreRepository(tmp_path/'core.db')
        project=core.create_project('Project','work')
        chat=core.create_conversation('project_work','Selection',project_id=project['id'])
        run=core.start_model_run(conversation_id=chat['id'],content=CONDITION,idempotency_key='scope',
            provider_profile_id='provider',model_id='model')
        attempt=core.begin_run_attempt(run['id'])
        monitor=ExecutionMonitor(core)
        root=monitor.start_task(run_id=run['id'],project_id=project['id'])
        plans=TaskPlans(core)
        plans.create(root['id'],{'content':CONDITION,'attachments':[{'id':'source'}]})
        document=Document()
        document.add_heading('Guidance',1)
        document.add_paragraph('Formal operating requirement A')
        document.add_paragraph('Example: sample equipment')
        table=document.add_table(rows=1,cols=2)
        table.cell(0,0).text='Example continuation value'
        table.cell(0,1).text='Formal operating requirement B'
        payload=io.BytesIO();document.save(payload)
        workflow=DocumentWorkflow(core)
        template=workflow.register_template(run['id'],payload.getvalue(),'fixture.docx',source_id='source')
        actual_fields=inventory(payload.getvalue())['fields']
        SourceInventory(core).register(root['id'],'source',template['id'],template['sha256'],actual_fields,complete=True)
        if renewed_artifact:
            with core._connect() as db:db.execute("UPDATE task_inventory_sources SET artifact_id='earlier-inspection-artifact' WHERE task_id=?",(root['id'],))
        if source_mismatch:
            with core._connect() as db:db.execute("UPDATE task_inventory_sources SET source_hash='different-source-hash' WHERE task_id=?",(root['id'],))
        requirements={'source_scopes':[{'source_id':'source','operation':'review','scope':'selected','target_ids':[],
            'selector':{'kind':'selection_pending','condition':CONDITION},'reason':CONDITION}],
            'deliverables':[{'path':'review.md','format':'markdown'}],'success_criteria':['Review only requested content']}
        goals=plans.capture_requirements(root['id'],requirements,expected_revision=0)
        with core._connect() as db:db.execute('INSERT INTO task_goal_execution VALUES(?,?,?,?)',
            (root['id'],goals['revision'],goals['request_digest'],json.dumps({'_first_result_id':'immutable-first-read'})))
        service=runtime.ChatRunService.__new__(runtime.ChatRunService)
        service.repository=core;service.documents=workflow;service.execution_monitor=monitor;service.task_plans=plans
        profile=SimpleNamespace(id='provider',model_capabilities={'model':{'context_length':8192}})
        service._profile=lambda _:profile
        service._provider=lambda _:SimpleNamespace()
        service._profile_for_step=lambda p,s:p
        step={'id':'step','step_type':'final_response','protocol_id':'openai_chat'}
        service.execution_coordinator=SimpleNamespace(get_for_run=lambda _: {'steps':[step]})
        service.execution_repository=SimpleNamespace(update_step_usage=lambda *_:None)
        service.invocation_compiler=InvocationCompiler()
        service.rag_grounding=SimpleNamespace(runs={run['id']:{'project_id':project['id'],'generation':1}},
            validate=lambda _:None,document_claim=lambda *_:('',[]))
        service.rag=SimpleNamespace(project_state=lambda _: {'library_ids':[],'index_ids':[]})
        service._create_model_invocation_spec=lambda *args,**kwargs:model_spec(protocol_id='openai_chat',adapter_id='openai.chat',
            route_id='chat',allowed_tool_names=kwargs['allowed_tools'],max_output_tokens=kwargs['max_output_tokens'],
            response_format=kwargs['response_format'])
        invoked=[]
        async def invoke(run,plan,step,attempt,provider,messages,**kwargs):
            from agent.providers import ProviderError
            assert kwargs['apply_rag_contract'] is False
            if not schema_supported and kwargs.get('response_schema'):
                raise ProviderError('response_format_unsupported','Synthetic unsupported schema')
            if not schema_supported:
                assert '"decisions"' in messages[0]['content'] and '"context_refs"' in messages[0]['content']
            assert 'This is scope selection, not technical review' in messages[0]['content']
            supplied=json.loads(messages[1]['content']);invoked.append(supplied)
            return json.dumps(reply(supplied)),{},[]
        service._invoke_planned_step=invoke
        selected_seen=[]
        async def review(*args,**kwargs):
            selected_seen.extend(kwargs['target_ids'])
            return {'report':{'targets':[],'coverage':{'total':len(kwargs['target_ids'])}},'markdown':'Selected scope report'}
        monkeypatch.setattr('agent.document_review.review_template',review)
        service._review_template_planned=MethodType(runtime.ChatRunService._review_template_planned,service)
        with associate(monitor,root['id']):
            if source_mismatch:
                from agent.storage import CoreDataError
                with pytest.raises(CoreDataError) as caught:
                    await service._review_template_planned(run,attempt['id'],None,CONDITION,
                        template_id=template['id'],return_result=True)
                assert caught.value.code=='task_source_changed' and not invoked
                with core._connect() as db:
                    assert db.execute('SELECT source_hash FROM task_inventory_sources WHERE task_id=?',(root['id'],)).fetchone()[0]=='different-source-hash'
                    assert db.execute('SELECT count(*) FROM task_work_items').fetchone()[0]==0
                return
            result=await service._review_template_planned(run,attempt['id'],None,CONDITION,
                template_id=template['id'],return_result=True)
        expected=[f['id'] for f in actual_fields if f['text'].startswith('Formal')]
        assert selected_seen==expected and invoked
        assert result['report']['coverage']['total']==2
        with core._connect() as db:
            assert {row[0] for row in db.execute('SELECT target_id FROM task_work_items')}==set(expected)
            proof=json.loads(db.execute('SELECT payload_json FROM task_goal_execution').fetchone()[0])['_review_selection']
            assert proof['complete'] and set(proof['decisions'])=={f['id'] for f in actual_fields}
    asyncio.run(scenario())


def test_existing_source_hash_mismatch_fails_before_register_or_provider(tmp_path,monkeypatch):
    test_runtime_omitted_ids_resolves_complete_source_before_review_callback(tmp_path,monkeypatch,True,source_mismatch=True)


def test_same_source_hash_allows_renewed_inspection_artifact(tmp_path,monkeypatch):
    test_runtime_omitted_ids_resolves_complete_source_before_review_callback(tmp_path,monkeypatch,True,renewed_artifact=True)
