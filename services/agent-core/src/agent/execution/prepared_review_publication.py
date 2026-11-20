"""Publish the actual reviewed artifact through the normal project file tool."""
import json
from hashlib import sha256
from pathlib import PureWindowsPath

from agent.execution.goal_requirements import format_name
from agent.storage import CoreDataError
from agent.tool_contracts import ToolCall


def requested_review_path(current, goals):
    outputs = goals.get('deliverables', [])
    if len(outputs) != 1:
        return None
    item = outputs[0]
    name = item.get('path')
    if not name or name not in current['content']:
        return None
    path = PureWindowsPath(name)
    if path.drive or path.root or '..' in path.parts:
        return None
    if format_name(item.get('format')) != 'md' or path.suffix.lower() != '.md':
        return None
    return name


async def publish_requested_review(service, run, attempt_id, current, goals, receipt):
    run_id = run['id']
    name = requested_review_path(current, goals)
    artifact_id = receipt['artifact_id']
    artifact = service.documents.get(run_id, artifact_id)
    manifest = artifact['manifest']
    state = service.rag_grounding.runs.get(run_id)
    generation = state['generation'] if state is not None else None
    if (not name or artifact['kind'] != 'markdown' or receipt['format'] != artifact['kind']
            or artifact['sha256'] != receipt['artifact_sha256']
            or manifest.get('review_report', {}).get('template_sha256') != receipt['source_sha256']
            or manifest.get('rag_generation') != generation):
        raise CoreDataError('document_publish_unverified', '报告、来源或保存目标与本次审阅不一致。')
    if state is not None:
        service.rag_grounding.validate(run_id)
    body = service.documents.path_for(run_id, artifact_id).read_bytes().decode('utf-8')
    event = {'file_name': name, 'artifact_id': artifact_id}
    await service._emit(run_id, 'run.document_processing', {**event, 'phase': 'review_report_generated'})
    arguments = {'artifact_id': artifact_id, 'path': name}
    previous = next((op for op in reversed(service.tool_loop.repository.list_for_run(run_id))
                     if op['tool_name'] == 'document.export' and op['status'] == 'completed'
                     and op['arguments'] == arguments), None)
    if previous:
        saved = service.result_store.read(run_id, previous['id'], length=16000)
        try:
            publication = json.loads(saved['content'])
        except ValueError as error:
            raise CoreDataError('document_publish_unverified', '无法核验上次发布回执。') from error
    else:
        await service._emit(run_id, 'run.document_processing', {**event, 'phase': 'review_report_publishing'})
        call = ToolCall('prepared-review-export-' + artifact_id, 'document.export', arguments)
        publication = await service._application_document_operation(run, attempt_id, call)
    if (not isinstance(publication, dict) or publication.get('artifact_id') != artifact_id
            or PureWindowsPath(publication.get('path') or '') != PureWindowsPath(name)):
        raise CoreDataError('document_publish_unverified', '实际发布的报告或路径与请求不一致。')
    service.documents.record_publication(run_id, artifact_id, publication)
    service.documents.verify_publication(run_id, artifact_id)
    await service._emit(run_id, 'run.document_processing', {**event, 'phase': 'review_report_verified'})
    if state is not None:
        service.rag_grounding.validate(run_id)
    root = service.execution_monitor.start_task(run_id=run_id)
    complete = service.task_plans.completion(root['id'], require_declared=True)['ready']
    notice = '' if complete else '\n\n报告已保存，部分审阅事项仍待核对。'
    answer = body + notice + '\n\n[打开文件](<' + publication['path'] + '>)'
    run['_review_reports'] = [body]
    run['_verified_named_review_uses_rag'] = state is not None
    run['_verified_named_review_hash'] = sha256(answer.encode()).hexdigest()
    return answer
