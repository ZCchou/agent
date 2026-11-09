import asyncio
from pathlib import Path

from agent.ocr.pipeline import OcrPipeline
from agent.rag.service import RagService
from agent.storage import CoreRepository
from test_ocr_pipeline import Client
from test_rag_service import Engines


def test_auto_pdf_import_publishes_indexes_and_binds_only_requested_project(tmp_path):
    async def run():
        core = CoreRepository(tmp_path / 'agent.db')
        project = core.create_project('OCR project', 'research')
        service = RagService(core, Engines())
        service.ocr = OcrPipeline(service.repository, client=Client())
        library = (await service.handle('library.create', {'name': 'DO160', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']
        source = tmp_path / 'DO-160G.pdf'
        source.write_bytes(b'%PDF-fixture')
        result = await service.handle('document.import', {'library_id': library['id'], 'paths': [str(source)],
            'ocr_mode': 'auto', 'profile_id': 'fixture', 'auto_index': True,
            'auto_apply_project_id': project['id']})
        for _ in range(200):
            if not service._tasks:
                break
            await asyncio.sleep(.01)
        assert not service._tasks, [(key, task.done()) for key, task in service._tasks.items()]
        job = service.repository.job(result['job']['id'])
        assert job['status'] == 'completed', job
        documents = service.repository.list_libraries()[0]['documents']
        assert documents and all(document['status'] == 'confirmed' for document in documents)
        found = await service.search(project['id'], 'Category A temperature')
        assert any('-55' in item['text'] for item in found['evidence'])
        assert any(item['locator'].get('extraction_method') == 'ocr' for item in found['evidence'])
        await service.close()
    asyncio.run(run())
