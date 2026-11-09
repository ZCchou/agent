import asyncio
import json

from agent.ocr.pipeline import OcrPipeline
from agent.rag.repository import RagRepository
from agent.storage import CoreRepository


class Client:
    calls = 0
    fail = False

    def available(self):
        return {'available': True}

    async def request(self, action, path, **params):
        if action == 'scan':
            return {'pages': [
                {'page': 1, 'text': '4.3 Equipment categories and applicability.', 'needs_ocr': False},
                {'page': 2, 'text': '4-9', 'needs_ocr': True},
            ]}
        self.calls += 1
        if self.fail:
            raise RuntimeError('fixture OCR failure')
        return {'page': 2, 'regions': [{'id': 'p2-r0', 'kind': 'table', 'text': 'Category A | -55 C',
            'bbox': [0, 0, 1, 1], 'review_status': 'machine', 'extraction_method': 'ocr'}], 'status': 'completed'}

    async def close(self):
        pass


def setup(tmp_path):
    core = CoreRepository(tmp_path / 'agent.db')
    repository = RagRepository(core)
    library = repository.create_library({'name': 'OCR test', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'})
    job = repository.new_job('document.import', library['id'])
    repository.update_job(job['id'], status='running')
    digest, original = repository.save_original(b'%PDF-fixture', '.pdf')
    client = Client()
    pipeline = OcrPipeline(repository, client=client)
    return repository, job, digest, original, pipeline, client


def test_ocr_merges_native_and_image_pages_and_keeps_locators(tmp_path):
    async def run():
        repository, job, digest, original, pipeline, client = setup(tmp_path)
        result = await pipeline.process(job, original, digest, {'profile_id': 'fixture'})
        assert client.calls == 1
        assert any('-55' in chunk['text'] and chunk['locator']['page'] == 2 for chunk in result['chunks'])
        assert any('Equipment' in chunk['text'] for chunk in result['chunks'])
        assert result['ocr']['failed_pages'] == []
        again = await pipeline.process(job, original, digest, {'profile_id': 'fixture'})
        assert client.calls == 1
        assert again['chunks'] == result['chunks']
    asyncio.run(run())


def test_failed_page_keeps_other_text_searchable_and_records_gap(tmp_path):
    async def run():
        repository, job, digest, original, pipeline, client = setup(tmp_path)
        client.fail = True
        result = await pipeline.process(job, original, digest, {'profile_id': 'fixture'})
        assert result['ocr']['failed_pages'] == [2]
        assert any('Equipment' in chunk['text'] for chunk in result['chunks'])
        assert any('2' in warning for warning in result['warnings'])
    asyncio.run(run())
