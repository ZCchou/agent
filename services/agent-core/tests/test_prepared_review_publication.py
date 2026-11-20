import asyncio
import json
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent.execution.prepared_review_publication import publish_requested_review
from agent.storage import CoreDataError


@pytest.fixture
def case(tmp_path):
    path = tmp_path / '模板审阅结果.md'
    body = '# 审阅\n水压项依据不足。'
    path.write_bytes(body.encode())
    digest = sha256(body.encode()).hexdigest()
    artifact = dict(id='a', kind='markdown', sha256=digest, status='generated',
                    manifest={'rag_generation': 4, 'review_report': {'template_sha256': 'src'}})
    docs = Mock()
    docs.get.return_value = artifact
    docs.path_for.return_value = path
    service = Mock()
    service.documents = docs
    service._emit = AsyncMock()
    service.task_plans.completion.return_value = {'ready': True}
    service.rag_grounding.runs = {'r': {'generation': 4}}
    service.execution_monitor.start_task.return_value = {'id': 'task'}
    service.tool_loop.repository.list_for_run.return_value = []
    host = {'artifact_id': 'a', 'sha256': digest, 'path': path.name,
            'absolute_path': str(path), 'status': 'published'}
    service._application_document_operation = AsyncMock(return_value=host)
    return SimpleNamespace(service=service, run={'id': 'r'}, current={'content': f'审阅后保存为“{path.name}”。'},
        goals={'deliverables': [{'format': 'md', 'path': path.name}]},
        receipt={'artifact_id': 'a', 'artifact_sha256': digest, 'format': 'markdown', 'source_sha256': 'src'},
        host=host, path=path, artifact=artifact, body=body)


def publish(c):
    return asyncio.run(publish_requested_review(c.service, c.run, 'attempt', c.current, c.goals, c.receipt))


def test_exact_generated_content_is_published_through_host(case):
    result = publish(case)
    call = case.service._application_document_operation.await_args.args[-1]
    assert call.name == 'document.export'
    assert call.arguments == {'artifact_id': 'a', 'path': case.path.name}
    assert case.body in result and case.path.name in result
    case.service.documents.verify_publication.assert_called_once_with('r', 'a')
    phases = [call.args[2]['phase'] for call in case.service._emit.await_args_list]
    assert phases == ['review_report_generated', 'review_report_publishing', 'review_report_verified']


def test_resume_after_host_write_reuses_receipt(case):
    case.service.tool_loop.repository.list_for_run.return_value = [{
        'tool_name': 'document.export', 'status': 'completed',
        'arguments': {'artifact_id': 'a', 'path': case.path.name},
        'id': 'prior-export'}]
    case.service.result_store.read.return_value = {'content': json.dumps(case.host)}
    publish(case)
    case.service._application_document_operation.assert_not_awaited()
    case.service.documents.record_publication.assert_called_once()


def test_coverage_is_separate_from_file_publication(case):
    case.service.task_plans.completion.return_value = {'ready': False}
    result = publish(case)
    assert case.body in result and '部分审阅事项仍待核对' in result
    case.service.documents.verify_publication.assert_called_once_with('r', 'a')


@pytest.mark.parametrize('problem', ['changed', 'stale', 'wrong_path', 'wrong_artifact'])
def test_cannot_claim_invalid_publication(case, problem):
    if problem == 'changed':
        case.receipt['artifact_sha256'] = 'other'
    elif problem == 'stale':
        case.artifact['manifest']['rag_generation'] = 3
    elif problem == 'wrong_path':
        case.host['path'] = 'other.md'
    elif problem == 'wrong_artifact':
        case.host['artifact_id'] = 'other'
    with pytest.raises(CoreDataError):
        publish(case)
    assert '_verified_named_review_hash' not in case.run


def test_report_bytes_and_recovery_use_persistent_artifact(tmp_path, case):
    from test_document_workflow import workflow, template_bytes

    repo, run, docs = workflow(tmp_path)
    body = '# 审阅\r\n' + ('依据与适用范围需人工复核。\n' * 1500)
    template = docs.register_template(run['id'], template_bytes(), '原件.docx')
    artifact = docs.create(run['id'], body, 'markdown')
    docs.record_review(run['id'], artifact['id'], dict(template_id=template['id'],
        template_sha256=template['sha256'], coverage={'completed': 3}, review_status='pending_human_review'))
    manifest = docs.get(run['id'], artifact['id'])['manifest']
    with repo._connect() as db:
        db.execute('UPDATE document_artifacts SET manifest_json=? WHERE id=?',
                   (json.dumps({**manifest, 'rag_generation': 4}), artifact['id']))
    case.service.documents = docs
    case.run = run
    case.receipt.update(artifact_id=artifact['id'], artifact_sha256=artifact['sha256'],
                        source_sha256=template['sha256'])
    case.service.rag_grounding.runs = {run['id']: {'generation': 4}}
    case.path.write_bytes(docs.path_for(run['id'], artifact['id']).read_bytes())
    case.host.update(artifact_id=artifact['id'], sha256=artifact['sha256'])
    result = publish(case)
    assert result.startswith(body)
    assert docs.verify_publication(run['id'], artifact['id'])['sha256'] == sha256(case.path.read_bytes()).hexdigest()
    case.path.write_bytes(b'changed')
    case.service.tool_loop.repository.list_for_run.return_value = [{'id': 'saved', 'tool_name': 'document.export',
        'status': 'completed', 'arguments': {'artifact_id': artifact['id'], 'path': case.path.name}}]
    case.service.result_store.read.return_value = {'content': json.dumps(case.host)}
    with pytest.raises(CoreDataError, match='变化'):
        publish(case)
    assert case.service._application_document_operation.await_count == 1

def test_source_only_review_remains_publishable_without_a_rag_generation(case):
    case.service.rag_grounding.runs.clear()
    case.artifact['manifest'].pop('rag_generation')
    case.artifact['manifest']['review_report']['evidence_participation'] = 'no_evidence'
    result = publish(case)
    assert case.body in result
    case.service.rag_grounding.validate.assert_not_called()
    case.service.documents.verify_publication.assert_called_once_with('r', 'a')
    assert case.run['_verified_named_review_uses_rag'] is False
