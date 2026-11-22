import asyncio
import pytest

from agent.rag.service import RagService
from agent.storage import CoreDataError, CoreRepository
from test_rag_service import Engines, ready, wait_job


class CountCalls(Engines):
    def __init__(self):
        self.calls = []
        self.device = 'cpu'

    def list_models(self):
        return [{**record, 'device': self.device} for record in super().list_models()]

    async def embed(self, model_id, texts, query=False):
        if query:
            self.calls.append('embed')
        return await super().embed(model_id, texts, query=query)

    async def rerank(self, model_id, query, texts):
        self.calls.append('rerank')
        return await super().rerank(model_id, query, texts)


def test_later_runs_reuse_only_authorized_ranking(tmp_path):
    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        models = CountCalls()
        service = RagService(core, models)
        try:
            project = core.create_project('Review', 'work')['id']
            library = (await service.handle('library.create', {'name': 'Local standard',
                'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            version = await ready(service, library, 'DO-160F', '5.2 温度变化速率。\n\n6.3 湿热试验程序。')
            assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
            binding = {'library_id': library, 'project_id': project, 'enabled': True}
            await service.handle('library.bind', binding)
            options = {'retrieval_scope':'focused', 'constraint_text':'结合DO-160F审阅附件第10.1节温度变化。'}
            first = await service.search(project, '温度变化', run_id='run-a', **options)
            assert models.calls == ['embed', 'rerank']
            # The published record is loaded again; presentation metadata is not cached.
            with core._connect() as db:
                db.execute("UPDATE rag_versions SET title='DO-160F 原文' WHERE id=?", (version,))
            progress_events = []
            async def capture(event):
                progress_events.append(event)
            second = await service.search(project, '温度变化', run_id='run-b', progress=capture, **options)
            assert {r['run_id'] for r in second['evidence']} == {'run-b'}
            assert any(isinstance(event, dict) and event.get('stage')=='retrieval_cache_hit' for event in progress_events)
            assert models.calls == ['embed', 'rerank']
            assert first['search_id'] != second['search_id']
            assert {r['id'] for r in first['evidence']}.isdisjoint(r['id'] for r in second['evidence'])
            assert all(r['title']=='DO-160F 原文' for r in second['evidence'])
            assert [r['text_hash'] for r in first['evidence']] == [r['text_hash'] for r in second['evidence']]
            assert [r['score'] for r in first['evidence']] == [r['score'] for r in second['evidence']]
            await service.search(project, '温度变化', limit=1, **options)
            assert models.calls == ['embed', 'rerank'] * 2
            await service.search(project, '温度变化', constraint_text='DO-160F 第5.2节', retrieval_scope='focused')
            assert models.calls[-1] == 'rerank' and len(models.calls)==5
            models.device='cuda'
            await service.search(project, '温度变化', **options)
            assert models.calls[-2:]==['embed','rerank'] and len(models.calls)==7
            await service.handle('library.bind', {**binding,'enabled':False})
            with pytest.raises(CoreDataError, match='authorized'):
                await service.search(project, '温度变化', **options)
            assert len(models.calls)==7
            await service.handle('library.bind', binding)
            await service.search(project, '温度变化', **options)
            assert len(models.calls)==9 and models.calls[-2:] == ['embed','rerank']
        finally:
            await service.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('fault', ['timeout', 'crash', 'cancel', 'invalid'])
def test_review_ranking_faults_preserve_execution_boundaries(tmp_path, fault):
    async def exercise():
        from agent.rag.grounding import evidence_for_model
        from agent.rag.review_queries import search_review_units
        core = CoreRepository(tmp_path / 'agent.db')
        models = CountCalls()
        service = RagService(core, models)
        try:
            project = core.create_project('Review', 'work')['id']
            library = (await service.handle('library.create', {'name': 'Standard',
                'embedding_model_id':'embed','reranker_model_id':'rank'}))['library']['id']
            await ready(service, library, content='6.3 湿热条件。')
            assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
            await service.handle('library.bind', {'library_id':library,'project_id':project,'enabled':True})
            previous = models.rerank
            async def fail(*args):
                models.calls.append('failed_rank')
                if fault == 'cancel':
                    raise asyncio.CancelledError
                if fault == 'crash':
                    raise CoreDataError('rag_worker_crash','Stopped')
                if fault == 'invalid':
                    return [float('nan')]
                await asyncio.Event().wait()
            models.rerank = fail
            service.rerank_timeout = .02 if fault == 'timeout' else None
            options = {'retrieval_scope':'focused','constraint_text':'结合资料库审阅附件'}
            events=[]
            async def progress(event):
                events.append(event)
            async def search(query):
                return await service.search(project, query, progress=progress, **options)
            cache = {}
            generation = service.project_state(project)['generation']
            if fault in ('cancel','invalid'):
                with pytest.raises(asyncio.CancelledError if fault=='cancel' else CoreDataError):
                    await search('湿热')
                with core._connect() as db:
                    assert db.execute('SELECT count(*) FROM rag_evidence').fetchone()[0]==0
            else:
                result = await search_review_units(['湿热'], search, generation=generation, cache=cache)
                assert not cache and result['evidence']
                expected = 'rag_rerank_timeout' if fault == 'timeout' else 'rag_worker_crash'
                for item in result['evidence']:
                    assert item['ranking_error_code'] == expected
                    assert '初筛' in evidence_for_model(item)['warnings'][0]
                assert any(isinstance(event,dict) and event.get('stage')=='rerank_degraded' for event in events)
            assert not service._rankings and models.calls == ['embed','failed_rank']
            models.rerank = previous
            service.rerank_timeout = None
            recovered = await search_review_units(['湿热'], search, generation=generation, cache=cache)
            assert models.calls == ['embed','failed_rank','embed','rerank']
            assert all('ranking_error_code' not in item for item in recovered['evidence'])
            assert service._rankings and cache
        finally:
            await service.close()
    asyncio.run(exercise())
