import asyncio
import json
from contextlib import closing

from agent.rag.documents import _chunks
from agent.rag.service import RagService
from agent.storage import CoreRepository


class Models:
    def list_models(self):
        return [{'id': 'embed', 'role': 'embedding', 'digest': 'a' * 64, 'dimension': 3, 'status': 'ready'},
                {'id': 'rank', 'role': 'reranker', 'digest': 'b' * 64, 'status': 'ready'}]

    async def embed(self, model_id, texts, query=False):
        return [[1., .2, .3] for _ in texts]

    async def rerank(self, model_id, query, texts):
        return [1. for _ in texts]

    async def close(self):
        pass


async def finish(service, job_id):
    for _ in range(300):
        job = service.repository.job(job_id)
        if job['status'] not in {'queued', 'running'}:
            return job
        await asyncio.sleep(.01)
    raise AssertionError('task did not finish')


def test_empty_library_can_be_created_before_models_are_configured(tmp_path):
    async def run():
        models = Models()
        models.list_models = lambda: []
        service = RagService(CoreRepository(tmp_path / 'agent.db'), models)
        try:
            library = (await service.handle('library.create', {'name': 'New library'}))['library']
            assert library['name'] == 'New library'
            assert not library['embedding_model_id']
        finally:
            await service.close()
    asyncio.run(run())


def test_one_import_publishes_one_final_index_and_updates_bound_project(tmp_path):
    async def run():
        core = CoreRepository(tmp_path / 'agent.db')
        service = RagService(core, Models())
        project = core.create_project('Project', 'work')['id']
        library = (await service.handle('library.create', {'name': 'Rules', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
        path = tmp_path / 'rules.pdf'
        path.write_bytes(b'%PDF-fixture')
        async def process(job, original, digest, options, on_initial=None):
            native = _chunks([('4.1 native source', {'page': 1})], [])
            native['ocr'] = {'coverage': 'partial', 'pending_pages': [2], 'total_pages': 2}
            if on_initial:
                await on_initial(native)
            final = _chunks([('4.1 native source\n4.2 recognized equipment requirement', {'page': 2})], [])
            final['ocr'] = {'coverage': 'complete', 'ocr_pages': 1, 'total_pages': 2, 'failed_pages': []}
            return final
        service.ocr.process = process
        try:
            result = await service.handle('document.import', {'library_id': library, 'paths': [str(path)], 'ocr_mode': 'auto', 'auto_index': True})
            job = await finish(service, result['job']['id'])
            assert job['status'] == 'completed', job
            top = (await service.handle('job.list', {}))['jobs']
            assert len([item for item in top if not item.get('parent_id')]) == 1
            with closing(core._connect()) as c:
                assert c.execute('SELECT count(*) FROM rag_indexes').fetchone()[0] == 1
                assert c.execute('SELECT count(*) FROM rag_documents').fetchone()[0] == 1
                active = c.execute('SELECT active_index_id FROM rag_libraries WHERE id=?', (library,)).fetchone()[0]
                assert c.execute('SELECT index_id FROM rag_bindings WHERE project_id=?', (project,)).fetchone()[0] == active
                versions = json.loads(c.execute('SELECT version_ids FROM rag_indexes WHERE id=?', (active,)).fetchone()[0])
                assert len(versions) == 1
            assert job['phase'] == 'completed'
            assert job['termination_source'] is None
        finally:
            await service.close()
    asyncio.run(run())


def test_task_cancellation_records_request_origin(tmp_path):
    async def run():
        service = RagService(CoreRepository(tmp_path / 'agent.db'), Models())
        try:
            library = (await service.handle('library.create', {'name': 'Rules', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            job = service.repository.new_job('document.import', library)
            await service.handle('job.cancel', {'job_id': job['id']})
            record = service.repository.job(job['id'])
            assert record['termination_source'] == 'user_action'
            detail = await service.handle('job.detail', {'job_id': job['id']})
            assert any(event['action'] == 'cancel' for event in detail['events'])
        finally:
            await service.close()
    asyncio.run(run())

def test_resume_after_model_configuration_reuses_validated_parse(tmp_path):
    class Parser:
        calls = 0
        async def parse(self, path):
            self.calls += 1
            return _chunks([(path.read_text(encoding='utf-8'), {'paragraph':1})], [])
        async def close(self):
            pass
    async def run():
        core=CoreRepository(tmp_path/'agent.db')
        parser=Parser()
        service=RagService(core,Models(),parser=parser)
        library=service.repository.create_library({'name':'Empty'})['id']
        path=tmp_path/'rules.txt';path.write_text('4.1 Equipment requirements',encoding='utf-8')
        try:
            first=await service.handle('document.import',{'library_id':library,'paths':[str(path)]})
            stopped=await finish(service,first['job']['id'])
            assert stopped['phase']=='needs_attention'
            assert parser.calls==1
            await service.handle('library.update',{'library_id':library,'embedding_model_id':'embed','reranker_model_id':'rank'})
            await service.handle('job.resume',{'job_id':stopped['id']})
            assert (await finish(service,stopped['id']))['status']=='completed'
            assert parser.calls==1
            assert service.repository.documents(library)['total']==1
        finally:
            await service.close()
    asyncio.run(run())


def test_repeated_concurrent_submission_is_idempotent(tmp_path):
    async def run():
        service=RagService(CoreRepository(tmp_path/'agent.db'),Models())
        library=(await service.handle('library.create',{'name':'Rules'}))['library']['id']
        path=tmp_path/'rules.txt';path.write_text('4.1 Equipment requirements',encoding='utf-8')
        params={'library_id':library,'paths':[str(path)],'request_key':'one-intent'}
        try:
            first,second=await asyncio.gather(service.handle('document.import',params),service.handle('document.import',params))
            assert first['job']['id']==second['job']['id']
            assert (await finish(service,first['job']['id']))['status']=='completed'
            assert len([j for j in service.repository.jobs() if j['kind']=='import.batch'])==1
        finally:
            await service.close()
    asyncio.run(run())


def test_library_test_does_not_grant_project_evidence_access(tmp_path):
    async def run():
        core=CoreRepository(tmp_path/'agent.db');service=RagService(core,Models())
        library=(await service.handle('library.create',{'name':'Rules'}))['library']['id']
        path=tmp_path/'rules.txt';path.write_text('4.1 Equipment requirements',encoding='utf-8')
        try:
            first=await service.handle('document.import',{'library_id':library,'paths':[str(path)]})
            assert (await finish(service,first['job']['id']))['status']=='completed'
            assert (await service.handle('library.search',{'library_id':library,'query':'equipment'}))['evidence']
            project=core.create_project('Unbound','work')['id']
            from agent.storage import CoreDataError
            import pytest
            with pytest.raises(CoreDataError):
                await service.search(project,'equipment')
        finally:
            await service.close()
    asyncio.run(run())

def test_excluded_document_stays_excluded_on_later_import(tmp_path):
    async def run():
        service=RagService(CoreRepository(tmp_path/'agent.db'),Models())
        library=(await service.handle('library.create',{'name':'Rules'}))['library']['id']
        files=[]
        for number in range(3):
            path=tmp_path/f'rules-{number}.txt';path.write_text(f'4.{number} Equipment requirement {number}',encoding='utf-8');files.append(path)
        try:
            first=await service.handle('document.import',{'library_id':library,'paths':[str(p) for p in files[:2]]})
            assert (await finish(service,first['job']['id']))['status']=='completed'
            documents=service.repository.documents(library)['documents']
            kept=documents[0]['version_id'];excluded=documents[1]['version_id']
            index=await service.handle('index.build',{'library_id':library,'version_ids':[kept]})
            assert (await finish(service,index['job']['id']))['status']=='completed'
            assert excluded not in service.repository.adopted_versions(library)
            later=await service.handle('document.import',{'library_id':library,'paths':[str(files[2])]})
            assert (await finish(service,later['job']['id']))['status']=='completed'
            assert excluded not in service.repository.adopted_versions(library)
        finally:
            await service.close()
    asyncio.run(run())


def test_restart_resume_preserves_explicit_project_unbind(tmp_path):
    class BlockingParser:
        def __init__(self):
            self.entered=asyncio.Event()
        async def parse(self,path):
            self.entered.set()
            await asyncio.Event().wait()
        async def close(self):
            pass
    async def run():
        core=CoreRepository(tmp_path/'agent.db');parser=BlockingParser()
        service=RagService(core,Models(),parser=parser)
        project=core.create_project('Project','work')['id']
        library=(await service.handle('library.create',{'name':'Rules'}))['library']['id']
        path=tmp_path/'rules.txt';path.write_text('4.1 Equipment requirements',encoding='utf-8')
        first=await service.handle('document.import',{'library_id':library,'paths':[str(path)],'auto_apply_project_id':project})
        await parser.entered.wait()
        await service.handle('library.bind',{'library_id':library,'project_id':project,'enabled':False})
        await service.close()
        restarted=RagService(core,Models())
        try:
            await restarted.handle('job.resume',{'job_id':first['job']['id']})
            assert (await finish(restarted,first['job']['id']))['status']=='completed'
            assert not restarted.project_state(project)['enabled']
        finally:
            await restarted.close()
    asyncio.run(run())

def test_separate_changed_sources_with_same_filename_remain_independent(tmp_path):
    async def run():
        service=RagService(CoreRepository(tmp_path/'agent.db'),Models())
        library=(await service.handle('library.create',{'name':'Rules'}))['library']['id']
        paths=[]
        for number in range(2):
            folder=tmp_path/f'folder{number}';folder.mkdir();path=folder/'rules.txt'
            path.write_text(f'4.{number} first source {number}',encoding='utf-8');paths.append(path)
        try:
            first=await service.handle('document.import',{'library_id':library,'paths':[str(p) for p in paths]})
            assert (await finish(service,first['job']['id']))['status']=='completed'
            for number,path in enumerate(paths):path.write_text(f'5.{number} changed source {number}',encoding='utf-8')
            second=await service.handle('document.import',{'library_id':library,'paths':[str(p) for p in paths],'decisions':{str(p):'separate' for p in paths}})
            assert (await finish(service,second['job']['id']))['status']=='completed'
            assert service.repository.documents(library)['total']==4
        finally:await service.close()
    asyncio.run(run())


def test_reviewed_version_has_independent_withdrawal_and_retry_identity(tmp_path):
    async def run():
        service=RagService(CoreRepository(tmp_path/'agent.db'),Models())
        library=(await service.handle('library.create',{'name':'Rules'}))['library']['id']
        path=tmp_path/'rules.txt';path.write_text('4.1 Equipment requirements',encoding='utf-8')
        try:
            first=await service.handle('document.import',{'library_id':library,'paths':[str(path)]})
            assert (await finish(service,first['job']['id']))['status']=='completed'
            original=service.repository.documents(library)['documents'][0]['version_id']
            reviewed=await service.handle('document.metadata',{'version_id':original,'title':'Reviewed rules','revision':'R1','source':'Official','document_number':'DO-160','warnings_acknowledged':True})
            assert (await finish(service,reviewed['job']['id']))['status']=='completed'
            assert service.repository.job(reviewed['job']['id'])['payload']['version_ids']==[reviewed['version_id']]
            await service.handle('document.withdraw',{'version_id':original})
            assert service.repository.preview(reviewed['version_id'])['version']['status']=='confirmed'
        finally:await service.close()
    asyncio.run(run())


def test_library_removal_records_its_termination_origin(tmp_path):
    async def run():
        service=RagService(CoreRepository(tmp_path/'agent.db'),Models())
        library=(await service.handle('library.create',{'name':'Rules'}))['library']['id']
        try:
            job=service.repository.new_job('import.batch',library)
            await service.handle('library.remove',{'library_id':library})
            assert service.repository.job(job['id'])['termination_source']=='library_removal'
        finally:await service.close()
    asyncio.run(run())

def test_successful_reprocess_resolves_the_previous_attention_task(tmp_path):
    async def run():
        service=RagService(CoreRepository(tmp_path/'agent.db'),Models())
        library=service.repository.create_library({'name':'Empty'})['id']
        path=tmp_path/'rules.txt';path.write_text('4.1 Equipment requirements',encoding='utf-8')
        try:
            first=await service.handle('document.import',{'library_id':library,'paths':[str(path)]})
            assert (await finish(service,first['job']['id']))['status']=='interrupted'
            await service.handle('library.update',{'library_id':library,'embedding_model_id':'embed','reranker_model_id':'rank'})
            second=await service.handle('document.import',{'library_id':library,'paths':[str(path)],'decisions':{str(path):'reprocess'}})
            assert (await finish(service,second['job']['id']))['status']=='completed'
            assert service.repository.job(first['job']['id']).get('resolved_at')
            assert service.repository.overview()['libraries'][0]['attention_count']==0
        finally:await service.close()
    asyncio.run(run())
