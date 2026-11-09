import asyncio
import pytest

from agent.ocr.pipeline import OcrPipeline
from agent.rag.service import RagService
from agent.storage import CoreRepository
from test_ocr_pipeline import Client, setup
from test_rag_service import Engines, ready


async def until(predicate):
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(.005)
    raise AssertionError('Timed out waiting for fixture state')


async def scene(tmp_path, client, engines=None):
    core = CoreRepository(tmp_path / 'agent.db')
    project = core.create_project('OCR', 'research')['id']
    service = RagService(core, engines or Engines())
    service.ocr = OcrPipeline(service.repository, client=client)
    library = (await service.handle('library.create', {'name': 'OCR', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-fixture')
    return service, project, library, source


@pytest.mark.parametrize('manual', [False, True])
def test_stale_initial_index_drains_latest_ocr_rebuild(tmp_path, manual):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        class SlowEngines(Engines):
            async def embed(self, *args, **kwargs):
                started.set()
                await release.wait()
                return await super().embed(*args, **kwargs)
        class AfterIndexClient(Client):
            async def request(self, action, path, **params):
                if action == 'recognize':
                    await started.wait()
                return await super().request(action, path, **params)
        service, project, library, source = await scene(tmp_path, AfterIndexClient(), SlowEngines())
        try:
            if manual:
                version = await ready(service, library)
                await service.handle('index.build', {'library_id': library, 'version_ids': [version]})
            result = await service.handle('document.import', {'library_id': library, 'paths': [str(source)], 'ocr_mode': 'auto', 'profile_id': 'fixture', 'auto_apply_project_id': project})
            await until(lambda: service.repository.job(result['job']['id'])['status'] == 'completed')
            release.set()
            await until(lambda: not service._tasks)
            assert service.repository.list_libraries()[0]['active_index_id']
            assert (await service.search(project, 'temperature'))['evidence']
        finally:
            release.set()
            await service.close()
    asyncio.run(run())


def test_withdrawal_cancels_active_page_and_releases_pipeline(tmp_path):
    async def run():
        started, cancelled = asyncio.Event(), asyncio.Event()
        class BlockingClient(Client):
            async def request(self, action, path, **params):
                if action == 'recognize':
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cancelled.set()
                        raise
                return await super().request(action, path, **params)
        service, _, library, source = await scene(tmp_path, BlockingClient())
        try:
            await service.handle('document.import', {'library_id': library, 'paths': [str(source)], 'ocr_mode': 'auto', 'profile_id': 'fixture', 'auto_index': False})
            await started.wait()
            version = service.repository.list_libraries()[0]['documents'][0]['version_id']
            await service.handle('document.withdraw', {'version_id': version})
            await until(cancelled.is_set)
            await until(lambda: not service._tasks)
            assert not service.ocr._lock.locked()
        finally:
            await service.close()
    asyncio.run(run())


def test_user_unbind_is_preserved_after_final_ocr_index(tmp_path):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        class BlockingClient(Client):
            async def request(self, action, path, **params):
                if action == 'recognize':
                    started.set()
                    await release.wait()
                return await super().request(action, path, **params)
        service, project, library, source = await scene(tmp_path, BlockingClient())
        try:
            await service.handle('document.import', {'library_id': library, 'paths': [str(source)], 'ocr_mode': 'auto', 'profile_id': 'fixture', 'auto_apply_project_id': project})
            await started.wait()
            await until(lambda: service.project_state(project)['enabled'])
            await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': False})
            release.set()
            await until(lambda: not service._tasks)
            assert not service.project_state(project)['enabled']
        finally:
            release.set()
            await service.close()
    asyncio.run(run())


def test_missing_ocr_runtime_uses_managed_parser(tmp_path):
    async def run():
        _, job, digest, original, pipeline, _ = setup(tmp_path)
        class MissingClient(Client):
            def available(self):
                return {'available': False}
        class Parser:
            called = False
            async def parse(self, path):
                self.called = True
                return {'chunks': [], 'warnings': []}
        parser = Parser()
        pipeline.client = MissingClient()
        pipeline.parser = parser
        await pipeline.process(job, original, digest, {})
        assert parser.called
    asyncio.run(run())
