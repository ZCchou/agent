import asyncio
import json

import pytest

from agent.document_attachments import DocumentAttachments
from agent.document_contracts import validate_document_call
from agent.storage import CoreDataError, CoreRepository
from test_document_attachments import source_attachment


class Parser:
    cache_identity = 'parser-1'
    calls = 0

    async def parse(self, path):
        self.calls += 1
        return {'chunks': [{'text': 'long content ' * 1000, 'locator': {'kind': 'pdf', 'page': 2, 'heading_path': ['Power']}}], 'warnings': ['OCR missing']}


class Ocr:
    cache_identity = 'ocr-1'

    async def close(self):
        pass


def test_read_pages_sections_pagination_integrity_and_identity(tmp_path):
    async def scenario():
        attachment = source_attachment(tmp_path)
        parser = Parser()
        service = DocumentAttachments(CoreRepository(tmp_path / 'agent.db'), parser=parser, ocr=Ocr())
        kwargs = dict(physical_page=2, section='power', max_characters=1200, limit=2)
        first = await service.read(attachment, **kwargs)
        assert first['warnings'] == ['OCR missing']
        assert first['blocks'][0]['locator']['block_id'] == 'block-1'
        assert first['trust'] == 'untrusted'
        bodies = []
        page = first
        while True:
            assert len(json.dumps(page, ensure_ascii=False)) <= 1200
            bodies.extend(block['text'] for block in page['blocks'])
            if page['next_offset'] is None:
                break
            page = await service.read(attachment, offset=page['next_offset'], **kwargs)
        assert ''.join(bodies).strip() == ('long content ' * 1000).strip()
        assert parser.calls == 1
        assert (await service.read(attachment, physical_page=1))['total'] == 0
        parser.cache_identity = 'parser-2'
        await service.read(attachment)
        assert parser.calls == 2
        service.ocr.cache_identity = 'ocr-2'
        await service.read(attachment)
        assert parser.calls == 3
        artifact = next(iter(reversed(service._read_cache.values())))
        (tmp_path / artifact['storage_key']).write_text('tampered', encoding='utf-8')
        with pytest.raises(CoreDataError, match='完整性'):
            await service.read(attachment)
        (tmp_path / attachment['storage_key']).write_bytes(b'changed!')
        with pytest.raises(CoreDataError):
            await service.read(attachment)
    asyncio.run(scenario())


@pytest.mark.parametrize('arguments', [{}, {'attachment_id': 'a', 'offset': True}, {'attachment_id': 'a', 'limit': 101}, {'attachment_id': 'a', 'physical_page': 0}, {'attachment_id': 'a', 'section': []}, {'attachment_id': 'a', 'path': 'x'}])
def test_read_rejects_malformed_arguments(arguments):
    with pytest.raises(CoreDataError):
        validate_document_call('document.read_attachment', arguments)


def test_unknown_configuration_reparses_and_ignores_caller_artifact(tmp_path):
    async def scenario():
        attachment = source_attachment(tmp_path)
        attachment['artifacts'].append({'kind': 'text.utf8', 'storage_key': '../evil'})
        parser = Parser()
        ocr = Ocr()
        ocr.cache_identity = None
        service = DocumentAttachments(CoreRepository(tmp_path / 'agent.db'), parser=parser, ocr=ocr)
        await service.read(attachment)
        await service.read(attachment)
        assert parser.calls == 2
        with pytest.raises(CoreDataError):
            await service.read(attachment, max_characters=256)
    asyncio.run(scenario())


def test_literal_warning_body_remains_pageable(tmp_path):
    async def scenario():
        parser = Parser()
        body = ('WARNING: ' + 'ordinary source text ' * 15 + '\n') * 10
        async def parse(path):
            return {'chunks': [{'text': body, 'locator': {'kind': 'pdf', 'page': 1}}], 'warnings': ['OCR missing']}
        parser.parse = parse
        service = DocumentAttachments(CoreRepository(tmp_path / 'agent.db'), parser=parser, ocr=Ocr())
        attachment = source_attachment(tmp_path)
        offset, parts = 0, []
        while True:
            result = await service.read(attachment, offset=offset, max_characters=1200)
            assert result['warnings'] == ['OCR missing']
            parts.extend(block['text'] for block in result['blocks'])
            offset = result['next_offset']
            if offset is None:
                break
        assert ''.join(parts).strip() == body.strip()
    asyncio.run(scenario())


@pytest.mark.parametrize('frozen', [False, True])
def test_builtin_runtime_identity_and_frozen_paging(tmp_path, monkeypatch, frozen):
    import hashlib
    import sys
    from agent.document_parser import ParserService
    from agent.ocr.client import OcrClient
    from agent.rag.repository import RagRepository

    async def scenario():
        core = CoreRepository(tmp_path / 'agent.db')
        data_dir = RagRepository(core).data_dir
        parser, ocr = ParserService(data_dir), OcrClient(data_dir)
        runtime = tmp_path / 'ocr-runtime'
        executable = runtime / 'python' / 'python.exe'
        executable.parent.mkdir(parents=True)
        dependency = runtime / 'dependency.dll'
        executable.write_bytes(b'ocr executable')
        dependency.write_bytes(b'first runtime')
        def manifest():
            value = {'schema_version': 1, 'profile': 'ocr-preview-cpu-v1', 'files': {
                'python/python.exe': hashlib.sha256(executable.read_bytes()).hexdigest(),
                'dependency.dll': hashlib.sha256(dependency.read_bytes()).hexdigest()}}
            (runtime / 'runtime.json').write_text(json.dumps(value), encoding='utf-8')
        manifest()
        core_executable = tmp_path / 'core.exe'
        core_executable.write_bytes(b'core executable')
        monkeypatch.setattr(sys, 'executable', str(core_executable))
        monkeypatch.setattr(sys, 'frozen', frozen, raising=False)
        parser.command = [str(core_executable), '--document-worker'] if frozen else [str(core_executable), '-m', 'agent.document_parser.worker']
        monkeypatch.setattr(ocr, 'available', lambda: {'available': True, 'command': [str(executable), '-B', '-m', 'agent.ocr.worker']})
        calls = []
        async def parse(path):
            calls.append(path)
            return {'chunks': [{'text': 'reading ' * 1000, 'locator': {'kind': 'docx', 'paragraph': 1}}], 'warnings': []}
        monkeypatch.setattr(parser, 'parse', parse)
        service = DocumentAttachments(core, parser=parser, ocr=ocr)
        attachment = source_attachment(tmp_path)
        first = await service.read(attachment, max_characters=1200)
        assert first['next_offset'] is not None
        second = await service.read(attachment, offset=first['next_offset'], max_characters=1200)
        assert len(calls) == 1
        assert first['text_artifact']['artifact_id'] == second['text_artifact']['artifact_id']
        dependency.write_bytes(b'changed runtime')
        manifest()
        await service.read(attachment, max_characters=1200)
        assert len(calls) == 2
        executable.write_bytes(b'changed OCR executable')
        manifest()
        await service.read(attachment, max_characters=1200)
        assert len(calls) == 3
        core_executable.write_bytes(b'changed core')
        await service.read(attachment, max_characters=1200)
        assert len(calls) == 4
        dependency.write_bytes(b'unverified runtime')
        await service.read(attachment, max_characters=1200)
        await service.read(attachment, max_characters=1200)
        assert len(calls) == 6
        await service.close()
    asyncio.run(scenario())
