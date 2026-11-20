import asyncio

import pytest
from docx import Document

from agent.storage import CoreDataError
from test_document_workflow import workflow
from test_document_plan import source_document


@pytest.mark.parametrize('contract_error', ['rag_quote_invalid', 'rag_citation_invalid'])
@pytest.mark.parametrize('interruption', ['generation', 'replay'])
def test_same_run_obsolete_quote_rebuilds_from_original_and_resumes_after_interruption(tmp_path, contract_error, interruption):
    _, run, service = workflow(tmp_path)
    source = source_document(service, run)
    changed = False
    interrupt = True
    generated = []
    original_fill = service.fill
    def fill(*args, **kwargs):
        nonlocal interrupt
        if changed and interrupt and interruption == 'replay':
            interrupt = False
            raise RuntimeError('interrupt replay')
        return original_fill(*args, **kwargs)
    service.fill = fill

    async def generate(task):
        nonlocal interrupt
        if task['stage'] == 'plan':
            return {'targets': [{'target_id': identity, 'field_type': 'instruction'} for identity in ('p-2', 'p-5')]}
        identities = [field['target_id'] for field in task['fields']]
        generated.extend(identities)
        if changed and interrupt and interruption == 'generation':
            interrupt = False
            assert identities == ['p-2']
            raise RuntimeError('interrupt regenerated field')
        return {'fields': [{'target_id': identity,
            'text': 'New supported text' if changed and identity == 'p-2' else 'Old applied text ' + identity,
            'evidence': [{'id': identity, 'quote_id': 'new' if changed and identity == 'p-2' else 'old'}]}
            for identity in identities]}

    def evidence(text, citations):
        if changed and any(item['id'] == 'p-2' and item['quote_id'] == 'old' for item in citations):
            raise CoreDataError(contract_error, 'Old quote contract no longer matches',
                {'locator_issue': 'illustration_scope_mismatch'} if contract_error == 'rag_citation_invalid' else None)
        return text, [{'id': item['id'], 'quote_id': item['quote_id'], 'revision': 'A'} for item in citations]

    first = asyncio.run(service.fill_planned(run['id'], source['id'], 'fill', generate, evidence_resolver=evidence))
    original_bytes = service.path_for(run['id'], source['id']).read_bytes()
    old_bytes = service.path_for(run['id'], first['id']).read_bytes()
    changed = True
    with pytest.raises(RuntimeError, match='interrupt'):
        asyncio.run(service.fill_planned(run['id'], source['id'], 'fill', generate, evidence_resolver=evidence))
    cached = service.read_plan(run['id'], source['id'], 'fill')
    assert cached['status'] == 'filling'
    bad, good = cached['targets']
    assert bad['status'] == 'pending' and bad['resume_issue'] == contract_error
    assert good['status'] == 'completed'
    assert cached['artifact_id'] != first['id']
    if interruption == 'replay':
        assert cached['artifact_id'] is None
        assert cached['evidence_replay_pending']
    else:
        rebuilt = Document(service.path_for(run['id'], cached['artifact_id']))
        assert rebuilt.paragraphs[2].text == '说明：填写本章试验目的，删除示例。'
        assert rebuilt.paragraphs[5].text == 'Old applied text p-5'
    final = asyncio.run(service.fill_planned(run['id'], source['id'], 'fill', generate, evidence_resolver=evidence))
    text = [p.text for p in Document(service.path_for(run['id'], final['id'])).paragraphs]
    assert text[2] == 'New supported text'
    assert text[5] == 'Old applied text p-5'
    assert generated == ['p-2', 'p-5'] + (['p-2'] if interruption == 'generation' else []) + ['p-2']
    # A further same-run resume neither regenerates nor reuses the stale artifact.
    again = asyncio.run(service.fill_planned(run['id'], source['id'], 'fill', generate, evidence_resolver=evidence))
    assert again['id'] == final['id']
    assert service.path_for(run['id'], source['id']).read_bytes() == original_bytes
    assert service.path_for(run['id'], first['id']).read_bytes() == old_bytes


@pytest.mark.parametrize('code', ['rag_generation_changed', 'rag_evidence_invalid', 'rag_citation_invalid', 'permission_denied'])
def test_same_run_resume_does_not_regenerate_when_authority_is_invalid(tmp_path, code):
    _, run, service = workflow(tmp_path)
    source = source_document(service, run)
    revoked = False
    calls = []
    async def generate(task):
        calls.append(task['stage'])
        if task['stage'] == 'plan':
            return {'targets': [{'target_id': 'p-2', 'field_type': 'instruction'}]}
        return {'fields': [{'target_id': 'p-2', 'text': 'Supported text', 'evidence': [{'id': 'e', 'quote_id': 'q'}]}]}
    def evidence(text, citations):
        if revoked:
            raise CoreDataError(code, 'Authority changed')
        return text, [{'id': 'e', 'quote_id': 'q'}]
    first = asyncio.run(service.fill_planned(run['id'], source['id'], 'fill', generate, evidence_resolver=evidence))
    revoked = True
    for _ in range(2):
        with pytest.raises(CoreDataError) as caught:
            asyncio.run(service.fill_planned(run['id'], source['id'], 'fill', generate, evidence_resolver=evidence))
        assert caught.value.code == code
    assert calls == ['plan', 'fill']
    cached = service.read_plan(run['id'], source['id'], 'fill')
    assert cached['artifact_id'] == first['id']
