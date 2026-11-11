import asyncio
import hashlib
from pathlib import Path

import pytest

from agent.storage import CoreDataError, CoreRepository


def source_attachment(root, content=b"document", name="report.docx"):
    path = root / "message-attachments" / "source.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    source = {"artifact_id": "source", "kind": "document.source", "processor_id": "builtin.document",
              "processor_version": "office-document-v1", "media_type": "application/octet-stream",
              "byte_size": len(content), "sha256": digest, "storage_key": "message-attachments/source.bin"}
    return {"id": "doc", "kind": "text", "name": name, "media_type": "application/octet-stream",
            "byte_size": len(content), "sha256": digest, "storage_key": source["storage_key"], "artifacts": [source]}


class Parser:
    async def parse(self, path):
        return {"chunks": [{"text": "Equipment requirements", "locator": {"kind": "docx", "paragraph": 2}}],
                "warnings": ["Verify diagrams"]}


def test_document_attachment_extracts_immutable_text_with_locations(tmp_path, monkeypatch):
    from agent.document_attachments import DocumentAttachments
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    core = CoreRepository(tmp_path / "agent.db")
    attachment = source_attachment(tmp_path)
    async def scenario():
        service = DocumentAttachments(core, parser=Parser())
        extracted = await service.extract(attachment)
        artifact = next(a for a in extracted["artifacts"] if a["kind"] == "text.utf8")
        text = (tmp_path / artifact["storage_key"]).read_text(encoding="utf-8")
        assert "Equipment requirements" in text
        assert '"paragraph": 2' in text
        assert "Verify diagrams" in text
        assert (tmp_path / attachment["storage_key"]).read_bytes() == b"document"
        assert hashlib.sha256(text.encode()).hexdigest() == artifact["sha256"]
        await service.close()
    asyncio.run(scenario())


def test_document_source_artifacts_are_accepted_without_relaxing_text_limits(tmp_path):
    attachment = source_attachment(tmp_path)
    assert CoreRepository._validate_attachments([attachment])
    text = {**attachment, "media_type": "text/plain", "artifacts": [], "byte_size": 2 * 1024 ** 2}
    with pytest.raises(CoreDataError) as error:
        CoreRepository._validate_attachments([text])
    assert error.value.code == "attachment_too_large"


def test_changed_original_is_rejected_before_parser(tmp_path):
    from agent.document_attachments import DocumentAttachments
    core = CoreRepository(tmp_path / "agent.db")
    attachment = source_attachment(tmp_path)
    (tmp_path / attachment["storage_key"]).write_bytes(b"changed")
    async def scenario():
        service = DocumentAttachments(core, parser=Parser())
        with pytest.raises(CoreDataError) as error:
            await service.extract(attachment)
        assert error.value.code == "attachment_changed"
        await service.close()
    asyncio.run(scenario())


def test_document_excerpt_keeps_source_offsets_and_signals_partial_reading():
    from agent.document_attachments import document_excerpt
    text = "intro\n" * 1000 + "\nPOWER INPUT REQUIREMENTS\n28 V DC\n" + "tail\n" * 1000
    content, start, end = document_excerpt(text, "POWER INPUT", 400)
    assert "POWER INPUT REQUIREMENTS" in content
    assert content == text[start:end]
    assert end - start <= 400
    assert end < len(text)


def test_document_excerpt_ranks_body_and_target_heading_above_locator_metadata():
    import json
    from agent.document_attachments import document_excerpt, document_excerpt_locator
    def block(number, heading, text):
        locator = {'kind': 'docx', 'block_id': f'block-{number}', 'paragraph': number, 'heading_path': heading}
        return '[SOURCE ' + json.dumps(locator, ensure_ascii=False) + ']\n' + text + '\n'
    text = block(1, ['振动试验', '索引'], '参见后文。')
    text += block(2, ['温度试验', '试验目的'], '验证高温下的功能。')
    text += block(3, ['振动试验', '试验目的'], '验证振动条件下的功能、耐久性与结构完整性。')
    excerpt, start, end = document_excerpt(text, '填写振动试验的试验目的', 90)
    assert '耐久性与结构完整性' in excerpt
    assert excerpt == text[start:end]
    assert document_excerpt_locator(text, start)['paragraph'] == 3
    title = block(0, ['机载设备环境鉴定试验大纲模板'], '机载设备环境鉴定试验大纲模板')
    excerpt, _, _ = document_excerpt(title + text,
        '请依据当前项目资料库填写机载设备环境鉴定试验大纲模板中振动试验的试验目的，说明需要验证设备的能力', 90)
    assert '耐久性与结构完整性' in excerpt


def test_document_locator_retains_template_standard_version_from_body():
    from agent.document_attachments import document_excerpt_locator
    text = '[SOURCE {"block_id":"one"}]\n依据 RTCA DO-160G。\n'
    text += '[SOURCE {"block_id":"two","heading_path":["振动试验"]}]\n验证性能。'
    locator = document_excerpt_locator(text, text.index('验证性能'))
    assert locator['referenced_standards'] == ['DO-160G']


def test_document_excerpt_expands_matching_heading_into_section_body():
    import json
    from agent.document_attachments import document_excerpt
    def block(number, body):
        locator = {'block_id': str(number), 'heading_path': ['振动试验', '试验目的']}
        return '[SOURCE ' + json.dumps(locator, ensure_ascii=False) + ']\n' + body + '\n'
    text = block(1, '试验目的') + block(2, '设备性能与耐久性应满足要求。') + block(3, 'x' * 2000)
    excerpt, start, end = document_excerpt(text, '填写振动试验的试验目的', 400)
    assert '设备性能与耐久性应满足要求。' in excerpt
    assert excerpt == text[start:end]


def test_normal_chat_entry_extracts_document_before_model_dispatch(tmp_path, monkeypatch):
    from agent.runtime import ChatRunService
    from agent.context_engine.service import ContextEngineService
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    core = CoreRepository(tmp_path / "agent.db")
    attachment = source_attachment(tmp_path)
    async def scenario():
        events = []
        async def sink(event):
            events.append(event)
        service = ChatRunService(core, sink)
        service.document_attachments.parser = Parser()
        try:
            result = await service.start({"content": "Read the equipment requirements", "idempotency_key": "read-document",
                "kind": "standard_chat", "category": "casual", "provider_profile_id": "builtin-demo", "model_id": "demo",
                "attachments": [attachment]})
            await service.tasks[result['run']['id']]
            run = core.get_model_run(result['run']['id'])
            assert run['status'] == 'completed', run
            packed = ContextEngineService(core).assemble(run)
            assert "Equipment requirements" in str(packed.messages)
            stored = core.get_run_conversation(run['id'])['messages'][0]['attachments'][0]
            assert any(item['kind'] == 'text.utf8' for item in stored['artifacts'])
            reference = core.resolve_attachment_references(run['conversation_id'], [{'attachment_id': stored['id']}])[0]
            assert 'source_start' not in reference
            assert 'source_end' not in reference
            explicit = core.resolve_attachment_references(run['conversation_id'], [
                {'attachment_id': stored['id'], 'source_start': 0, 'source_end': 20}])[0]
            assert explicit['source_end'] == 20
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_cancelled_conversion_is_reaped_before_extraction_unlocks(tmp_path):
    import threading
    from agent.document_attachments import DocumentAttachments
    core = CoreRepository(tmp_path / 'agent.db')
    started, stopped = threading.Event(), threading.Event()
    service = DocumentAttachments(core, parser=Parser())
    def convert(path, cancel):
        started.set()
        cancel.wait(5)
        stopped.set()
        raise CoreDataError('rag_office_conversion_cancelled', 'Cancelled')
    service._converted_pdf = convert
    async def scenario():
        task = asyncio.create_task(service._convert(Path('source.doc')))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert stopped.is_set()
        finally:
            await service.close()
    asyncio.run(scenario())


def test_schema26_upgrade_preserves_existing_attachment_records(tmp_path):
    import sqlite3
    path = tmp_path / 'agent.db'
    core = CoreRepository(path)
    attachment = source_attachment(tmp_path)
    attachment['artifacts'] = []
    run = core.start_model_run(content='legacy', idempotency_key='legacy', provider_profile_id='builtin-demo',
                              model_id='demo', kind='standard_chat', category='casual', attachments=[attachment])
    with sqlite3.connect(path) as connection:
        declaration = connection.execute("SELECT sql FROM sqlite_master WHERE name='attachment_artifacts'").fetchone()[0]
        declaration = declaration.replace('"attachment_artifacts"', 'attachment_artifacts_old', 1)
        declaration = declaration.replace("'document.source',", '')
        connection.execute(declaration)
        connection.execute('DROP TABLE attachment_artifacts')
        connection.execute('ALTER TABLE attachment_artifacts_old RENAME TO attachment_artifacts')
        connection.execute('UPDATE schema_info SET version=26')
    upgraded = CoreRepository(path)
    stored = upgraded.get_run_conversation(run['id'])['messages'][0]['attachments'][0]
    assert stored['sha256'] == attachment['sha256']
    assert stored['storage_key'] == attachment['storage_key']
    with upgraded._connect() as connection:
        assert connection.execute('SELECT version FROM schema_info').fetchone()[0] == 29
        assert "'document.source'" in connection.execute("SELECT sql FROM sqlite_master WHERE name='attachment_artifacts'").fetchone()[0]


def test_pdf_scans_images_and_uses_ocr_text(tmp_path):
    from agent.document_attachments import DocumentAttachments
    core = CoreRepository(tmp_path / 'agent.db')
    attachment = source_attachment(tmp_path, b'%PDF-1.7', 'scan.pdf')
    class Ocr:
        def available(self):
            return {'available': True}
        async def request(self, action, path, **params):
            return {'pages': [{'page': 1, 'text': 'Native equipment text', 'needs_ocr': True}]}
        async def close(self):
            pass
    async def scenario():
        service = DocumentAttachments(core, parser=Parser(), ocr=Ocr())
        try:
            extracted = await service.extract(attachment)
            artifact = next(a for a in extracted['artifacts'] if a['kind'] == 'text.utf8')
            text = (tmp_path / artifact['storage_key']).read_text(encoding='utf-8')
            assert 'Native equipment text' in text
            assert 'OCR model is not configured' in text
        finally:
            await service.close()
    asyncio.run(scenario())


def test_image_only_office_still_reaches_ocr(tmp_path):
    from agent.document_attachments import DocumentAttachments
    core = CoreRepository(tmp_path / 'agent.db')
    attachment = source_attachment(tmp_path, name='scan.docx')
    class EmptyParser:
        async def parse(self, path):
            raise CoreDataError('rag_document_no_text', 'No native text')
    async def scenario():
        service = DocumentAttachments(core, parser=EmptyParser())
        service._converted_pdf = lambda original, cancel: original
        async def recognized(original, progress):
            return {'chunks': [{'text': 'Scanned airworthiness clause', 'locator': {'page': 1}}], 'warnings': []}
        service._pdf = recognized
        try:
            extracted = await service.extract(attachment)
            artifact = next(a for a in extracted['artifacts'] if a['kind'] == 'text.utf8')
            assert 'Scanned airworthiness clause' in (tmp_path / artifact['storage_key']).read_text(encoding='utf-8')
        finally:
            await service.close()
    asyncio.run(scenario())


def test_ocr_page_failure_preserves_readable_native_text(tmp_path, monkeypatch):
    from agent.document_attachments import DocumentAttachments
    from agent.ocr.store import OcrModelStore
    monkeypatch.setattr(OcrModelStore, 'list_models', lambda self: {'models': [{'id': 'profile', 'status': 'ready'}]})
    core = CoreRepository(tmp_path / 'agent.db')
    attachment = source_attachment(tmp_path, b'%PDF-1.7', 'partial.pdf')
    class Ocr:
        def available(self):
            return {'available': True}
        async def request(self, action, path, **params):
            if action == 'scan':
                return {'pages': [{'page': 1, 'text': 'Readable native clause', 'needs_ocr': False},
                                  {'page': 2, 'text': '', 'needs_ocr': True}]}
            raise CoreDataError('ocr_page_failed', 'Recognition failed')
        async def close(self):
            pass
    async def scenario():
        service = DocumentAttachments(core, parser=Parser(), ocr=Ocr())
        try:
            extracted = await service.extract(attachment)
            artifact = next(a for a in extracted['artifacts'] if a['kind'] == 'text.utf8')
            content = (tmp_path / artifact['storage_key']).read_text(encoding='utf-8')
            assert 'Readable native clause' in content
            assert 'Physical page 2' in content and 'ocr_page_failed' in content
        finally:
            await service.close()
    asyncio.run(scenario())
