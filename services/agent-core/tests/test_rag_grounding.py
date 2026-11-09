import asyncio
import json

import pytest

from agent.storage import CoreDataError


@pytest.mark.parametrize('question', ['帮我翻译一下', '解释一下', 'Translate that into Chinese'])
def test_followup_reauthorizes_previous_answer_evidence_for_new_run(tmp_path, question):
    from agent.rag.grounding import RagGrounding
    from agent.rag.service import RagService
    from agent.storage import CoreRepository
    from test_rag_service import Engines, ready, wait_job

    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        project = core.create_project('Project', 'work')['id']
        service = RagService(core, Engines())
        try:
            library = (await service.handle('library.create', {'name': 'Rules', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
            version = await ready(service, library)
            assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
            prior = (await service.search(project, '失效', run_id='old-run'))['evidence'][0]
            g = RagGrounding(service)
            conversation = {'project_id': project, 'messages': [
                {'id': 'old-user', 'role': 'user', 'content': 'What are the safety requirements?'},
                {'id': 'old-answer', 'role': 'assistant', 'content': f"[Rules](rag-evidence:{prior['id']})"},
                {'id': 'report', 'role': 'system_event', 'content': 'Execution report'},
                {'id': 'failed-followup', 'role': 'user', 'content': '帮我翻译一下'},
                {'id': 'new-user', 'role': 'user', 'content': question},
            ]}
            async def unavailable_search(*args, **kwargs):
                raise AssertionError('A cited followup must reauthorize the prior evidence')
            service.search = unavailable_search
            await g.prepare({'id': 'new-run', 'user_message_id': 'new-user'}, conversation)
            current = g.evidence('new-run')[0]
            assert current['id'] != prior['id']
            assert current['run_id'] == 'new-run'
            assert current['version_id'] == version
            assert current['text'] == prior['text']
            service.validate_evidence(project, [current['id']], run_id='new-run')
            with pytest.raises(CoreDataError):
                service.validate_evidence(project, [prior['id']], run_id='new-run')
            g.admit('new-run', [current['id']])
            answer = json.dumps({'claims': [{'text': 'Translation', 'evidence': [{'id': current['id'], 'quote': current['text']}]}]})
            assert current['id'] in g.finalize('new-run', answer)
            historical_answer = answer.replace(current['id'], prior['id'])
            rendered = g.finalize('new-run', historical_answer)
            assert f"rag-evidence:{current['id']}" in rendered
            assert f"rag-evidence:{prior['id']}" not in rendered
            with pytest.raises(CoreDataError) as forged_error:
                g.finalize('new-run', json.dumps({'aliases': {'forged-id': current['id']}, 'claims': [{
                    'text': 'Forged', 'evidence': [{'id': 'forged-id', 'quote': current['text']}]}]}))
            assert forged_error.value.code == 'rag_citation_invalid'
            with pytest.raises(CoreDataError):
                g.admit('new-run', [])
            with pytest.raises(CoreDataError) as excluded_error:
                g.finalize('new-run', historical_answer)
            assert excluded_error.value.code == 'rag_citation_invalid'
            g.admit('new-run', [current['id']])
            invalid_quote_answer = json.loads(historical_answer)
            invalid_quote_answer['claims'][0]['evidence'][0]['quote'] = 'Forged quote'
            with pytest.raises(CoreDataError) as quote_error:
                g.finalize('new-run', json.dumps(invalid_quote_answer))
            assert quote_error.value.code == 'rag_quote_invalid'
            await service.handle('document.withdraw', {'version_id': version})
            with pytest.raises(CoreDataError):
                g.finalize('new-run', historical_answer)
            with pytest.raises(CoreDataError):
                await g.prepare({'id': 'retry-run', 'user_message_id': 'new-user'}, conversation)
        finally:
            await service.close()
    asyncio.run(exercise())


def test_followup_without_citations_searches_prior_question_and_answer():
    class SearchService(EvidenceService):
        async def search(self, project_id, query, **kwargs):
            assert '25.1309' in query
            assert 'System safety' in query
            return await super().search(project_id, query, **kwargs)
    from agent.rag.grounding import RagGrounding
    g = RagGrounding(SearchService())
    asyncio.run(g.prepare({'id': 'run', 'user_message_id': 'u2'}, {'project_id': 'project-1', 'messages': [
        {'id': 'u1', 'role': 'user', 'content': '25.1309 requirements'},
        {'id': 'a1', 'role': 'assistant', 'content': 'System safety'},
        {'id': 'u2', 'role': 'user', 'content': '解释一下'},
    ]}))


@pytest.mark.parametrize('current', ['解释一下', '翻译第16节'])
def test_followup_ignores_later_messages_and_explicit_new_topics(current):
    from agent.rag.followup import followup_context
    messages = [
        {'id': 'u1', 'role': 'user', 'content': 'Prior question'},
        {'id': 'a1', 'role': 'assistant', 'content': '[Old](rag-evidence:old-id)'},
        {'id': 'u2', 'role': 'user', 'content': current},
        {'id': 'a2', 'role': 'assistant', 'content': '[Later](rag-evidence:later-id)'},
    ]
    ids, query = followup_context(messages, 'u2')
    assert 'later-id' not in ids
    if current == '解释一下':
        assert ids == ['old-id']
    else:
        assert ids == []
        assert query == current


def test_followup_stops_at_unanswered_new_topic_after_system_report():
    from agent.rag.followup import followup_context
    messages = [
        {'id': 'u1', 'role': 'user', 'content': 'Prior question'},
        {'id': 'a1', 'role': 'assistant', 'content': '[Old](rag-evidence:old-id)'},
        {'id': 'report', 'role': 'system_event', 'content': 'Execution report'},
        {'id': 'pending', 'role': 'user', 'content': 'Explain Section 16 power input'},
        {'id': 'current', 'role': 'user', 'content': '帮我翻译一下'},
    ]
    assert followup_context(messages, 'current') == ([], '帮我翻译一下')


def test_reauthorization_aliases_cannot_be_supplied_by_search_results():
    class SearchService(EvidenceService):
        async def search(self, *args, **kwargs):
            return {**await super().search(*args, **kwargs), 'aliases': {'forged-id': 'evidence-1'}}
    from agent.rag.grounding import RagGrounding
    g = RagGrounding(SearchService())
    prepare(g)
    g.admit('run-1', ['evidence-1'])
    with pytest.raises(CoreDataError) as error:
        g.finalize('run-1', json.dumps({'claims': [{'text': 'Claim', 'evidence': [{'id': 'forged-id', 'quote': evidence()['text']}]}]}))
    assert error.value.code == 'rag_citation_invalid'


def test_citation_failure_details_identify_bad_id_without_claim_or_quote():
    g = grounding()
    prepare(g)
    g.admit('run-1', ['evidence-1'])
    with pytest.raises(CoreDataError) as error:
        g.finalize('run-1', json.dumps({'claims': [{'text': 'private claim', 'evidence': [{'id': 'stale-id', 'quote': 'private quote'}]}]}))
    assert error.value.details == {'citation_id': 'stale-id', 'allowed_evidence_ids': ['evidence-1']}


def test_failed_retry_prepare_discards_previous_admission():
    g = grounding()
    prepare(g)
    g.admit('run-1', ['evidence-1'])
    async def failed_search(*args, **kwargs):
        raise CoreDataError('rag_no_evidence', 'No evidence')
    g.service.search = failed_search
    with pytest.raises(CoreDataError):
        prepare(g)
    assert g.evidence('run-1') == []


def evidence():
    return {"id": "evidence-1", "version_id": "version-1", "library_id": "library-1",
            "title": "规章", "revision": "R1", "clause_number": "25.1",
            "locator": "第1页", "text": "申请人应提供试验报告。除另有规定外，应保存记录。", "text_hash": "hash"}


class EvidenceService:
    def project_state(self, project_id):
        return {"enabled": project_id == "project-1", "generation": 4, "library_ids": ["library-1"]}

    async def search(self, project_id, query, **kwargs):
        return {"evidence": [evidence()], "generation": 4, "index_ids": ["index-1"]}

    def assert_generation(self, project_id, generation):
        if generation != 4:
            raise CoreDataError("rag_generation_changed", "资料已变化")

    def validate_evidence(self, project_id, ids, run_id=None):
        return [evidence()]


def grounding():
    from agent.rag.grounding import RagGrounding
    return RagGrounding(EvidenceService())


def prepare(g):
    asyncio.run(g.prepare({"id": "run-1", "user_message_id": "message-1"}, {
        "project_id": "project-1", "messages": [{"id": "message-1", "content": "需要什么报告"}]}))


def test_rag_requires_admitted_exact_quotes_and_renders_source_link():
    g = grounding()
    prepare(g)
    answer = json.dumps({"claims": [{"text": "需要提交报告", "evidence": [{
        "id": "evidence-1", "quote": "申请人应提供试验报告。"}]}]}, ensure_ascii=False)
    with pytest.raises(CoreDataError, match="证据"):
        g.finalize("run-1", answer)
    g.admit("run-1", ["evidence-1"])
    result = g.finalize("run-1", answer)
    assert "需要提交报告" in result
    assert "rag-evidence:evidence-1" in result
    assert "草案" in result
    forged = answer.replace("申请人应提供试验报告。", "申请人无需提供报告。")
    with pytest.raises(CoreDataError):
        g.finalize("run-1", forged)


def test_rag_rejects_unknown_ids_unstructured_and_stale_generation():
    g = grounding()
    prepare(g)
    g.admit("run-1", ["evidence-1"])
    for answer in ["符合要求", '{"claims":[]}', '{"claims":[{"text":"符合","evidence":[{"id":"fake","quote":"a"}]}]}']:
        with pytest.raises(CoreDataError):
            g.finalize("run-1", answer)
    g.runs["run-1"]["generation"] = 3
    with pytest.raises(CoreDataError) as error:
        g.finalize("run-1", '{"claims":[]}')
    assert error.value.code == "rag_generation_changed"


def test_rag_rendered_claim_cannot_load_remote_images():
    g = grounding()
    prepare(g)
    g.admit("run-1", ["evidence-1"])
    answer = json.dumps({"claims": [{"text": "![图](https://evil.invalid/a.png)", "evidence": [{
        "id": "evidence-1", "quote": "申请人应提供试验报告。"}]}]})
    rendered = g.finalize("run-1", answer)
    assert r"\!\[图\]" in rendered


def test_rag_policy_rejects_cloud_proxy_redirect_routes():
    from agent.providers import ProviderProfile
    from agent.rag.grounding import require_local_profile
    for endpoint in ["https://cloud.invalid", "http://localhost:11434", "http://127.0.0.1.evil:1", "http://user:pass@127.0.0.1:1"]:
        with pytest.raises(CoreDataError):
            require_local_profile(ProviderProfile("p", "p", "ollama", endpoint))
    profile = require_local_profile(ProviderProfile("p", "p", "ollama", "http://127.0.0.1:11434"))
    assert profile.base_url == "http://127.0.0.1:11434"


def test_rag_context_budget_admits_only_bounded_external_evidence():
    from agent.context_engine.domain import RetrievalConfidence, TrustKind
    from agent.context_engine.packer import pack_context
    result = pack_context(system_message="safe", pinned_knowledge=[], checkpoint=None,
        history_turns=[], retrieved=[], retrieval_confidence=RetrievalConfidence.NONE,
        task_state={}, current_message={"id": "u", "content": "问题"}, context_generation=0,
        context_window=2000, input_target=1000, hard_limit=1500, output_reserve=500,
        rag_evidence=[evidence(), {**evidence(), "id": "too-large", "text": "X" * 10000}])
    sources = [item for item in result.items if item.source.source_type == "rag_chunk"]
    assert [item.source.source_id for item in sources if item.included] == ["evidence-1"]
    assert all(item.source.trust == TrustKind.EXTERNAL_UNTRUSTED for item in sources)
    assert "too-large" not in json.dumps(result.messages)
    assert "证据编号" in json.dumps(result.messages, ensure_ascii=False)


def test_rag_tool_contract_rejects_scope_escalation_and_bad_ranges():
    from agent.tool_contracts import ToolCall, validate_tool_call
    assert validate_tool_call(ToolCall("c", "rag.search", {"query": "条款"})).name == "rag.search"
    for name, args in [("rag.search", {"query": "a", "project_id": "other"}),
                       ("rag.search", {"query": "a", "limit": 1000}),
                       ("rag.read", {"evidence_id": "x", "offset": -1})]:
        with pytest.raises(CoreDataError):
            validate_tool_call(ToolCall("c", name, args))


def test_core_migrates_rag_and_management_protocol(tmp_path):
    from agent.storage import CoreRepository
    from agent.runtime import ChatRunService
    from agent.protocol import PROTOCOL_VERSION, handle_async_message

    async def scenario():
        repository = CoreRepository(tmp_path / "agent.db")
        with repository._connect() as connection:
            assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 28
        async def sink(event):
            pass
        service = ChatRunService(repository, sink)
        response = await handle_async_message({"v": PROTOCOL_VERSION, "id": "rag", "type": "rag.request",
            "params": {"action": "library.list", "params": {}}}, service)
        assert response.get("result") == {"libraries": []}, response
        await service.shutdown()
    asyncio.run(scenario())


def test_additional_retrieval_respects_cumulative_evidence_budget():
    g = grounding()
    prepare(g)
    g.admit("run-1", ["evidence-1"])
    g.runs["run-1"]["budget"] = 1
    with pytest.raises(CoreDataError) as error:
        asyncio.run(g.execute("run-1", "rag.search", {"query": "问题"}, 12000))
    assert error.value.code == "rag_context_too_small"


def test_compaction_of_rag_history_rejects_remote_target(tmp_path):
    from agent.runtime import ChatRunService
    from agent.storage import CoreRepository
    from agent.rag.service import RagService
    from test_rag_service import Engines
    core = CoreRepository(tmp_path / "agent.db")
    project = core.create_project("适航", "work")
    work = core.create_conversation("project_work", "问答", project_id=project["id"])
    run = core.start_model_run(content="条款", idempotency_key="c", provider_profile_id="cloud", model_id="m", conversation_id=work["id"])
    async def exercise():
        async def sink(event):
            pass
        service = ChatRunService(core, sink)
        service.rag = RagService(core, Engines())
        library = (await service.rag.handle("library.create", {"name": "规章", "embedding_model_id": "embed", "reranker_model_id": "rank"}))["library"]["id"]
        await service.rag.handle("library.bind", {"library_id": library, "project_id": project["id"], "enabled": True})
        service.configure_provider({"id": "cloud", "name": "cloud", "kind": "openai_compatible", "base_url": "https://cloud.invalid", "allowed_model_ids": ["m"]})
        with pytest.raises(CoreDataError) as error:
            service._resolve_compaction_target(run)
        assert error.value.code == "rag_local_model_required"
        await service.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('copy_kind', ['branch', 'copy', 'derive', 'copy_deleted', 'branch_clean'])
def test_copied_rag_history_keeps_local_policy_through_message_lineage(tmp_path, copy_kind):
    from agent.runtime import ChatRunService
    from agent.storage import CoreRepository
    from agent.rag.service import RagService
    from test_rag_service import Engines, ready, wait_job

    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        project = core.create_project('Source project', 'work')['id']
        target = core.create_project('Target project', 'work')['id']
        async def sink(event):
            pass
        service = ChatRunService(core, sink)
        service.rag = RagService(core, Engines())
        library = (await service.rag.handle('library.create', {'name': 'Regulations', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        await service.rag.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
        await ready(service.rag, library)
        assert (await wait_job(service.rag, await service.rag.handle('index.build', {'library_id': library})))['status'] == 'completed'
        if copy_kind == 'derive':
            source = core.create_conversation('standard_chat', 'Source', category='work')
        else:
            source = core.create_conversation('project_work', 'Source', project_id=project)
        run = core.start_model_run(content='Safety requirements', idempotency_key='source', provider_profile_id='cloud', model_id='m', conversation_id=source['id'])
        attempt = core.begin_run_attempt(run['id'])
        core.transition_model_run(run['id'], {'queued'}, 'generating')
        retrieved = await service.rag.search(project, '失效', run_id=run['id'])
        answer = retrieved['evidence'][0]['text']
        if copy_kind == 'copy_deleted':
            answer += '\n\n[Regulation](rag-evidence:' + retrieved['evidence'][0]['id'] + ')'
        core.complete_model_run(run['id'], attempt['id'], answer)
        await service.rag.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': False})

        if copy_kind == 'branch_clean':
            clean = core.start_model_run(content='Start before evidence', idempotency_key='clean-branch', provider_profile_id='cloud', model_id='m',
                                        conversation_id=source['id'], edit_message_id=run['user_message_id'])
            assert not service._rag_requires_local(core.get_conversation(clean['conversation_id']))
            await service.shutdown()
            return
        if copy_kind == 'branch':
            following = core.start_model_run(content='Follow up', idempotency_key='following', provider_profile_id='cloud', model_id='m', conversation_id=source['id'])
            core.transition_model_run(following['id'], {'queued'}, 'cancelled')
            for number in range(2):
                following = core.start_model_run(content='Edited follow up', idempotency_key=f'branch-{number}', provider_profile_id='cloud', model_id='m',
                                                conversation_id=following['conversation_id'], edit_message_id=following['user_message_id'])
                core.transition_model_run(following['id'], {'queued'}, 'cancelled')
            copied = core.get_conversation(following['conversation_id'])
        else:
            copied = core.derive_work(source['id'], target) if copy_kind == 'derive' else core.copy_work(source['id'], target)
            copied = core.copy_work(copied['id'], target)
        if copy_kind == 'copy_deleted':
            core.delete_conversation(source['id'])
            core.permanently_delete_trash('conversation', source['id'])

        assert not service.rag.project_state(copied['project_id'])['enabled']
        assert any(message['content'] == answer for message in copied['messages'])
        assert service._rag_requires_local(copied)
        service.configure_provider({'id': 'cloud', 'name': 'cloud', 'kind': 'openai_compatible', 'base_url': 'https://cloud.invalid', 'allowed_model_ids': ['m']})
        next_run = core.start_model_run(content='Explain copied evidence', idempotency_key='copied-followup', provider_profile_id='cloud', model_id='m', conversation_id=copied['id'])
        with pytest.raises(CoreDataError) as error:
            service._resolve_compaction_target(next_run)
        assert error.value.code == 'rag_local_model_required'
        await service.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('valid_hash', [True, False])
def test_parent_read_context_is_admitted_only_after_hash_validation(valid_hash):
    import hashlib
    from agent.rag.grounding import RagGrounding
    parent_quote = '除非经局方批准，否则不得省略验证试验。'
    class ParentService(EvidenceService):
        def read_evidence(self, project_id, evidence_id, offset=0, length=8000):
            return {**evidence(), 'content': evidence()['text'], 'parent_context': {
                'text': parent_quote, 'text_hash': hashlib.sha256(parent_quote.encode()).hexdigest() if valid_hash else 'invalid',
                'scope': 'clause', 'offset': 0, 'total_length': len(parent_quote), 'has_more': False}}
    g = RagGrounding(ParentService())
    prepare(g)
    g.admit('run-1', ['evidence-1'])
    if not valid_hash:
        with pytest.raises(CoreDataError) as error:
            asyncio.run(g.execute('run-1', 'rag.read', {'evidence_id': 'evidence-1'}, 12000))
        assert error.value.code == 'rag_context_integrity_invalid'
        assert parent_quote not in g.runs['run-1']['admitted']['evidence-1']
        return
    answer = json.dumps({'claims': [{'text': '需要保留例外条件', 'evidence': [{'id': 'evidence-1', 'quote': parent_quote}]}]})
    with pytest.raises(CoreDataError) as error:
        g.finalize('run-1', answer)
    assert error.value.code == 'rag_quote_invalid'
    asyncio.run(g.execute('run-1', 'rag.read', {'evidence_id': 'evidence-1'}, 12000))
    assert parent_quote in g.finalize('run-1', answer)
