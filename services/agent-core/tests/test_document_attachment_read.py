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
