import asyncio
import time

import pytest

from agent.rag.service import RagService
from agent.storage import CoreDataError, CoreRepository


class Engines:
    fail = False

    def list_models(self):
        return [{'id': 'embed', 'role': 'embedding', 'digest': 'a' * 64, 'dimension': 3, 'status': 'ready'},
                {'id': 'rank', 'role': 'reranker', 'digest': 'b' * 64, 'status': 'ready'}]

    async def embed(self, model_id, texts, query=False):
        if self.fail:
            raise RuntimeError('engine failed')
        return [[1.0, float('失效' in text), 0.1] for text in texts]

    async def rerank(self, model_id, query, texts):
        return [float('失效' in text) for text in texts]

    async def close(self):
        pass


def test_reranking_preparation_does_not_claim_that_a_model_is_loading(tmp_path):
    async def scenario():
        service = RagService(CoreRepository(tmp_path / 'agent.db'), Engines())
        project = service.repository.core.create_project('Progress', 'work')['id']
        phases = []

        async def progress(event):
            phases.append(event['stage'] if isinstance(event, dict) else event)

        try:
            library = (await service.handle('library.create', {
                'name': 'Progress', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            await ready(service, library, content='8.1 设备失效时的试验要求。')
            assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
            await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
            await service.search(project, '失效', progress=progress)
            assert 'preparing_rerank' in phases
            assert 'model_loading' not in phases
        finally:
            await service.close()

    asyncio.run(scenario())


def test_library_search_reports_cold_and_resident_embedding_progress(tmp_path, monkeypatch):
    import json
    import sys
    from agent.rag.models import ModelStore
    import agent.rag.models as model_module
    from test_rag_models import model_directory
    monkeypatch.setattr(model_module, 'system_memory', lambda: {'available_physical_bytes': 16 * 1024**3})

    script = tmp_path / 'search_worker.py'
    script.write_text('''import json
import sys
for line in sys.stdin:
    request = json.loads(line)
    texts = request['params'].get('texts', [])
    if request['action'] == 'probe':
        result = {'available': True}
    elif request['action'] == 'rerank_lengths':
        result = [len(text) for text in texts]
    elif request['action'] == 'embed':
        result = [[1.0] + [0.0] * 1023 for text in texts]
    else:
        result = [0.9 for text in texts]
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
''', encoding='utf-8')

    async def scenario():
        models = ModelStore(tmp_path / 'models')
        embedding = await models.import_model(model_directory(tmp_path / 'embedding'), 'bge-m3')
        rank_source = model_directory(tmp_path / 'reranker')
        (rank_source / 'config.json').write_text(json.dumps({'model_type': 'xlm-roberta',
            'architectures': ['XLMRobertaForSequenceClassification'], 'hidden_size': 1024, 'num_labels': 1}))
        reranker = await models.import_model(rank_source, 'bge-reranker-v2-m3')
        monkeypatch.setattr(models, '_command', lambda: [sys.executable, str(script)])
        core = CoreRepository(tmp_path / 'agent.db')
        service = RagService(core, models)
        events = []

        async def progress(event):
            events.append(event)

        try:
            for record in (embedding, reranker):
                probe = await models.probe(record['id'])
                assert probe['available'], probe
            project = core.create_project('Progress', 'work')['id']
            library = (await service.handle('library.create', {'name': 'Progress',
                'embedding_model_id': embedding['id'], 'reranker_model_id': reranker['id']}))['library']['id']
            await ready(service, library, content='8.1 设备失效时的试验要求。')
            assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
            await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
            await models.invalidate()
            await service.search(project, '失效', progress=progress)
            assert any(isinstance(event, dict) and event.get('stage') == 'model_loading'
                       and event.get('model_role') == 'embedding' for event in events)
            events.clear()
            await service.search(project, '失效', progress=progress)
            assert any(isinstance(event, dict) and event.get('stage') == 'embedding' for event in events)
            assert not any(isinstance(event, dict) and event.get('stage') == 'model_loading' for event in events)
        finally:
            await service.close()

    asyncio.run(scenario())


def test_explicit_subject_retrieval_excludes_preface_and_contents(tmp_path):
    class MisleadingScores(Engines):
        queries = []

        async def rerank(self, model_id, query, texts):
            self.queries.append(query)
            return [0.99 if '译者' in text else 0.01 for text in texts]

    async def exercise():
        engine = MisleadingScores()
        core = CoreRepository(tmp_path / 'agent.db')
        service = RagService(core, engine)
        project = core.create_project('测试', 'work')['id']
        try:
            library = (await service.handle('library.create', {'name': '测试', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            await ready(service, library, content='译者的话\n本书介绍适航审定与航空工业。\n\n图8-1 发动机振动试验…………8-10\n图8-2 发动机试验…………8-11\n\n8.1 发动机舱内设备的试验条件。')
            assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
            await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
            result = await service.search(project, '随机抽取一条适航审定的关于发动机的内容')
            assert [item['text'] for item in result['evidence']] == ['8.1 发动机舱内设备的试验条件。']
            assert engine.queries == ['发动机']
            preface = await service.search(project, '译者的话')
            assert any(item['text'].startswith('译者的话') for item in preface['evidence'])
            with pytest.raises(CoreDataError) as error:
                await service.search(project, '关于液压泵的条款')
            assert error.value.code == 'rag_no_evidence'
        finally:
            await service.close()
    asyncio.run(exercise())


async def ready(service, library, title='标准', content='25.1309 失效状态安全性要求'):
    result = await service.handle('document.paste', {'library_id': library, 'title': title, 'content': content})
    version_id = result['version']['id']
    await service.handle('document.confirm', {'version_id': version_id, 'revision': 'R1', 'source': '局方', 'document_number': 'CCAR-25', 'title': title, 'accept_warnings': True})
    return version_id


async def wait_job(service, result):
    job_id = result['job']['id']
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        jobs = (await service.handle('job.list', {}))['jobs']
        job = next(item for item in jobs if item['id'] == job_id)
        if job['status'] not in {'queued', 'running'}:
            return job
        await asyncio.sleep(.01)
    raise AssertionError('job did not finish')


def test_reauthorization_is_project_scoped_atomic_and_fresh_on_retry(tmp_path):
    from contextlib import closing
    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        project = core.create_project('Project', 'work')['id']
        other = core.create_project('Other', 'work')['id']
        service = RagService(core, Engines())
        try:
            library = (await service.handle('library.create', {'name': 'Rules', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
            await ready(service, library)
            assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
            old = (await service.search(project, '失效', run_id='old'))['evidence'][0]
            first = service.reauthorize_evidence(project, [old['id']], run_id='retry')['evidence'][0]
            second = service.reauthorize_evidence(project, [old['id']], run_id='retry')['evidence'][0]
            assert len({old['id'], first['id'], second['id']}) == 3
            with pytest.raises(CoreDataError):
                service.reauthorize_evidence(other, [old['id']], run_id='new')
            with closing(service.repository.connect()) as connection:
                before = connection.execute('SELECT count(*) FROM rag_searches').fetchone()[0]
            with pytest.raises(CoreDataError):
                service.reauthorize_evidence(project, [old['id'], 'missing'], run_id='new')
            with closing(service.repository.connect()) as connection:
                assert connection.execute('SELECT count(*) FROM rag_searches').fetchone()[0] == before
            await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': False})
            with pytest.raises(CoreDataError):
                service.reauthorize_evidence(project, [old['id']], run_id='new')
        finally:
            await service.close()
    asyncio.run(exercise())


def test_legacy_separator_chunks_are_filtered_during_search_and_reindex(tmp_path):
    from contextlib import closing
    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        project = core.create_project('Project', 'work')['id']
        service = RagService(core, Engines())
        try:
            library = (await service.handle('library.create', {'name': 'Rules', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
            await ready(service, library, content='Valid requirement.\n\nLegacy separator.')
            assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
            with closing(service.repository.connect()) as connection, connection:
                connection.execute("UPDATE rag_chunks SET text='| --- |' WHERE text='Legacy separator.'")
                connection.execute("UPDATE rag_indexes SET splitter_versions='[\"source-block-v2\"]'")
            result = await service.search(project, 'requirement')
            assert [item['text'] for item in result['evidence']] == ['Valid requirement.']
            assert any('Reimport' in warning for warning in result['warnings'])
            assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
            with closing(service.repository.connect()) as connection:
                assert connection.execute('SELECT chunk_count FROM rag_indexes ORDER BY created_at DESC LIMIT 1').fetchone()[0] == 1
        finally:
            await service.close()
    asyncio.run(exercise())


def test_office_import_uses_normal_parser_and_preserves_cell_locator(tmp_path):
    from test_office_documents import package, spreadsheet
    async def exercise():
        service = RagService(CoreRepository(tmp_path / 'agent.db'), Engines())
        try:
            library = (await service.handle('library.create', {'name': 'Sheets', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            path = package(tmp_path, '.xlsx', spreadsheet())
            job = await wait_job(service, await service.handle('document.import', {'library_id': library, 'paths': [str(path)]}))
            assert job['status'] == 'completed', job
            version = service.repository.list_libraries()[0]['documents'][0]['version_id']
            preview = service.repository.preview(version)
            assert preview['chunks'][0]['text'] == 'Shared text'
            assert preview['chunks'][0]['locator']['cell'] == 'B3'
        finally:
            await service.close()
    asyncio.run(exercise())


def test_isolation_confirmation_index_withdraw_generation(tmp_path):
    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        first = core.create_project('First', 'work')['id']
        second = core.create_project('Second', 'work')['id']
        service = RagService(core, Engines())
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        await service.handle('library.bind', {'library_id': library, 'project_id': first, 'enabled': True})
        pending = await service.handle('document.paste', {'library_id': library, 'title': '草稿', 'content': '未经确认的草稿'})
        assert pending['version']['status'] == 'pending'
        with pytest.raises(CoreDataError):
            await service.search(first, '草稿')
        version = await ready(service, library)
        built = await wait_job(service, await service.handle('index.build', {'library_id': library}))
        assert built['status'] == 'completed', built
        result = await service.search(first, '25.1309 失效', run_id='run-a')
        evidence = result['evidence'][0]
        assert evidence['version_id'] == version
        assert service.read_evidence(first, evidence['id'])['text']
        with pytest.raises(CoreDataError):
            service.read_evidence(second, evidence['id'])
        with pytest.raises(CoreDataError):
            await service.search(second, '失效')
        generation = result['generation']
        await service.handle('document.withdraw', {'version_id': version})
        with pytest.raises(CoreDataError):
            service.read_evidence(first, evidence['id'])
        with pytest.raises(CoreDataError):
            service.assert_generation(first, generation)
        with pytest.raises(CoreDataError):
            await service.search(first, '失效')
        await service.close()
    asyncio.run(exercise())


def test_failed_rebuild_keeps_previous_index_and_confirmation_immutable(tmp_path):
    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        project = core.create_project('Project', 'work')['id']
        engine = Engines()
        service = RagService(core, engine)
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
        version = await ready(service, library)
        with pytest.raises(CoreDataError):
            await service.handle('document.confirm', {'version_id': version, 'revision': 'changed', 'source': 'x', 'document_number': 'x', 'title': 'x'})
        assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
        before = service.project_state(project)
        engine.fail = True
        assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'failed'
        assert service.project_state(project) == before
        engine.fail = False
        assert (await service.search(project, '失效'))['evidence']
        await service.close()
    asyncio.run(exercise())


def test_cancelled_index_cannot_publish_late_and_restart_marks_interrupted(tmp_path):
    class DelayedEngine(Engines):
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, model_id, texts, query=False):
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                # Some native inference calls complete even after cancellation.
                pass
            return await super().embed(model_id, texts, query)

    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        engine = DelayedEngine()
        service = RagService(core, engine)
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        await ready(service, library)
        job = (await service.handle('index.build', {'library_id': library}))['job']
        await engine.entered.wait()
        await service.handle('job.cancel', {'job_id': job['id']})
        engine.release.set()
        await asyncio.sleep(.05)
        assert service.repository.job(job['id'])['status'] == 'cancelled'
        assert service.repository.job(job['id'])['termination_source'] == 'user_action'
        assert service.repository.list_libraries()[0]['active_index_id'] is None
        interrupted = service.repository.new_job('index.build', library)
        await service.close()
        restarted = RagService(core, Engines())
        assert restarted.repository.job(interrupted['id'])['status'] == 'interrupted'
        await restarted.close()
    asyncio.run(exercise())


def test_warnings_require_explicit_acceptance_and_library_removal_revokes(tmp_path):
    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        project = core.create_project('Project', 'work')['id']
        service = RagService(core, Engines())
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
        from agent.rag.documents import parse_text
        parsed = parse_text('25.1309 失效要求')
        parsed['warnings'] = ['Verify reading order']
        preview = service.repository.save_version(library, '标准', parsed, 'c' * 64, 'standard.pdf', 'application/pdf')
        params = {'version_id': preview['version']['id'], 'title': '标准', 'revision': 'R1', 'source': '局方', 'document_number': '25'}
        with pytest.raises(CoreDataError) as error:
            await service.handle('document.confirm', params)
        assert error.value.code == 'rag_warnings_unaccepted'
        await service.handle('document.confirm', {**params, 'warnings_acknowledged': True})
        assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
        result = await service.search(project, '失效', run_id='one')
        evidence_id = result['evidence'][0]['id']
        assert service.validate_evidence(project, [evidence_id], 'one')
        with pytest.raises(CoreDataError):
            service.validate_evidence(project, [evidence_id], 'two')
        await service.handle('library.remove', {'library_id': library})
        assert not service.project_state(project)['enabled']
        with pytest.raises(CoreDataError):
            service.read_evidence(project, evidence_id)
        await service.close()
    asyncio.run(exercise())


def test_library_model_change_keeps_previous_model_index_and_version_filter(tmp_path):
    class MultipleModels(Engines):
        def list_models(self):
            return super().list_models() + [{'id': 'new-embed', 'role': 'embedding', 'digest': 'd' * 64, 'dimension': 4, 'status': 'ready'}]

        async def embed(self, model_id, texts, query=False):
            vectors = await super().embed(model_id, texts, query)
            return [vector + [.5] for vector in vectors] if model_id == 'new-embed' else vectors

        def remove(self, model_id):
            return {'removed': model_id}

    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        project = core.create_project('Project', 'work')['id']
        service = RagService(core, MultipleModels())
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
        first = await ready(service, library, 'First', '25.1309 第一版失效要求')
        second = await ready(service, library, 'Second', '25.1309 第二版失效要求')
        assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
        old_binding = service.project_state(project)
        await service.handle('library.update', {'library_id': library, 'embedding_model_id': 'new-embed'})
        assert (await service.search(project, '失效', version_ids=[first]))['evidence'][0]['version_id'] == first
        with pytest.raises(CoreDataError):
            await service.search(project, '失效', version_ids=['outside'])
        assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
        assert service.project_state(project)['generation'] > old_binding['generation']
        assert service.project_state(project)['generation'] > old_binding['generation']
        assert (await service.handle('model.remove', {'model_id': 'embed'}))['removed'] == 'embed'
        assert (await service.search(project, '失效', version_ids=[second]))['evidence'][0]['version_id'] == second
        await service.close()
    asyncio.run(exercise())


def test_search_reranks_at_most_40_candidates_across_libraries(tmp_path):
    class CountingEngine(Engines):
        reranked = 0

        async def rerank(self, model_id, query, texts):
            self.reranked += len(texts)
            return await super().rerank(model_id, query, texts)

    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        project = core.create_project('Project', 'work')['id']
        engine = CountingEngine()
        service = RagService(core, engine)
        for number in range(2):
            library = (await service.handle('library.create', {'name': f'法规{number}', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
            await ready(service, library, content='\n\n'.join(f'25.{index} 失效安全条款{index}' for index in range(30)))
            assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
        assert len((await service.search(project, '失效'))['evidence']) == 8
        assert engine.reranked <= 40
        await service.close()
    asyncio.run(exercise())


def test_v24_migration_preserves_tool_records_reviews_and_indexes(tmp_path):
    import sqlite3
    from agent.migrations.v24_rag import migrate
    with sqlite3.connect(tmp_path / 'migration.db') as connection:
        connection.executescript('''
            CREATE TABLE projects(id TEXT PRIMARY KEY);
            CREATE TABLE tool_operations(id TEXT PRIMARY KEY,tool_name TEXT NOT NULL
                CHECK(tool_name IN ('fs.read','terminal.run','skills.load','skills.read_resource','skills.save')), payload TEXT);
            CREATE UNIQUE INDEX idx_existing_tool_name ON tool_operations(tool_name,id);
            CREATE TABLE tool_reviews(id TEXT PRIMARY KEY,operation_id TEXT REFERENCES tool_operations(id));
            INSERT INTO tool_operations VALUES ('old','skills.load','original');
            INSERT INTO tool_reviews VALUES ('review','old');
        ''')
        connection.commit()
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute('PRAGMA legacy_alter_table=ON')
        connection.execute('BEGIN IMMEDIATE')
        migrate(connection)
        connection.commit()
        assert connection.execute('SELECT * FROM tool_operations').fetchall() == [('old', 'skills.load', 'original')]
        assert connection.execute('SELECT * FROM tool_reviews').fetchall() == [('review', 'old')]
        connection.execute("INSERT INTO tool_operations VALUES ('new','rag.search','query')")
        connection.execute("INSERT INTO tool_operations VALUES ('read','rag.read','evidence')")
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='idx_existing_tool_name'").fetchone()


def test_immediate_shutdown_cancels_jobs_before_first_execution(tmp_path, recwarn):
    async def exercise():
        service = RagService(CoreRepository(tmp_path / 'agent.db'), Engines())
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        await ready(service, library)
        job = (await service.handle('index.build', {'library_id': library}))['job']
        await service.close()
        assert service.repository.job(job['id'])['status'] == 'interrupted'
        assert service.repository.job(job['id'])['termination_source'] == 'app_exit'
    asyncio.run(exercise())
    import gc
    gc.collect()
    assert not [warning for warning in recwarn if 'never awaited' in str(warning.message)]


def test_preview_repository_does_not_interrupt_existing_jobs(tmp_path):
    from agent.rag.repository import RagRepository
    core = CoreRepository(tmp_path / 'agent.db')
    repository = RagRepository(core)
    library = repository.create_library({'name': 'Library', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'})
    job = repository.new_job('index.build', library['id'])
    preview = RagRepository(core, recover_jobs=False)
    assert preview.job(job['id'])['status'] == 'queued'


def test_sqlite_failure_mid_publication_rolls_back_entire_revision(tmp_path):
    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        service = RagService(core, Engines())
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        await ready(service, library, content='25.1309 失效要求\n\n下一段要求')
        assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
        old = service.repository.list_libraries()[0]['active_index_id']
        from contextlib import closing
        with closing(service.repository.connect()) as connection, connection:
            connection.execute('''CREATE TRIGGER fail_index_insert BEFORE INSERT ON rag_index_chunks
                BEGIN SELECT RAISE(ABORT,'Simulated disk failure'); END''')
        assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'failed'
        assert service.repository.list_libraries()[0]['active_index_id'] == old
        with closing(service.repository.connect()) as connection:
            assert connection.execute('SELECT COUNT(*) FROM rag_indexes').fetchone()[0] == 1
            assert connection.execute('SELECT COUNT(*) FROM rag_index_chunks').fetchone()[0] == 2
        await service.close()
    asyncio.run(exercise())


def test_permanent_project_delete_cascades_evidence_without_deleting_library(tmp_path):
    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        project = core.create_project('Project', 'work')['id']
        service = RagService(core, Engines())
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
        await ready(service, library)
        assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
        evidence_id = (await service.search(project, '失效'))['evidence'][0]['id']
        core.delete_project(project)
        core.permanently_delete_trash('project', project)
        assert service.repository.list_libraries()[0]['id'] == library
        from contextlib import closing
        with closing(service.repository.connect()) as connection:
            assert not connection.execute('SELECT id FROM rag_evidence WHERE id=?', (evidence_id,)).fetchone()
            assert not connection.execute('PRAGMA foreign_key_check').fetchall()
        await service.close()
    asyncio.run(exercise())


def test_projects_automatically_adopt_ready_document_revision(tmp_path):
    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        first_project = core.create_project('First', 'work')['id']
        second_project = core.create_project('Second', 'work')['id']
        service = RagService(core, Engines())
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        await service.handle('library.bind', {'library_id': library, 'project_id': first_project, 'enabled': True})
        await service.handle('library.bind', {'library_id': library, 'project_id': second_project, 'enabled': True})
        source = tmp_path / 'standard.md'
        async def revision(number):
            previous = {document['version_id'] for document in service.repository.list_libraries()[0]['documents']}
            source.write_text(f'25.1309 第{number}版失效要求', encoding='utf-8')
            assert (await wait_job(service, await service.handle('document.import', {'library_id': library, 'paths': [str(source)], 'auto_index': False, 'decisions': {str(source): 'replace'}})))['status'] == 'completed'
            version = next(document['version_id'] for document in service.repository.list_libraries()[0]['documents'] if document['version_id'] not in previous)
            await service.handle('document.confirm', {'version_id': version, 'title': '标准', 'revision': str(number), 'source': '局方', 'document_number': 'CCAR-25'})
            return version
        first = await revision(1)
        assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
        before = service.project_state(first_project)
        first_evidence = (await service.search(first_project, '失效'))['evidence'][0]
        assert first_evidence['version_id'] == first
        second = await revision(2)
        with pytest.raises(CoreDataError) as error:
            await service.handle('index.build', {'library_id': library})
        assert error.value.code == 'rag_version_selection_required'
        with pytest.raises(CoreDataError):
            await service.handle('index.build', {'library_id': library, 'version_ids': [first, second]})
        assert (await wait_job(service, await service.handle('index.build', {'library_id': library, 'version_ids': [second]})))['status'] == 'completed'
        assert service.project_state(first_project)['generation'] > before['generation']
        assert (await service.search(first_project, '失效'))['evidence'][0]['version_id'] == second
        assert (await service.search(second_project, '失效'))['evidence'][0]['version_id'] == second
        await service.handle('library.bind', {'library_id': library, 'project_id': second_project, 'enabled': True})
        assert (await service.search(second_project, '失效'))['evidence'][0]['version_id'] == second
        assert (await service.search(first_project, '失效'))['evidence'][0]['version_id'] == second
        bindings = (await service.handle('library.list', {'project_id': second_project}))['libraries']
        assert bindings[0]['index_id'] == service.project_state(second_project)['index_ids'][0]
        with pytest.raises(CoreDataError):
            await service.search(first_project, '失效', version_ids=[first])
        await service.close()
    asyncio.run(exercise())


def test_paste_document_id_creates_version_of_existing_document(tmp_path):
    async def exercise():
        service = RagService(CoreRepository(tmp_path / 'agent.db'), Engines())
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        initial = await service.handle('document.paste', {'library_id': library, 'title': 'First', 'content': '第一版'})
        document_id = initial['version']['document_id']
        second = await service.handle('document.paste', {'library_id': library, 'document_id': document_id, 'title': 'Second', 'content': '第二版'})
        assert second['version']['document_id'] == document_id
        with pytest.raises(CoreDataError):
            await service.handle('document.paste', {'library_id': library, 'document_id': 'missing', 'title': 'Third', 'content': '第三版'})
        await service.close()
    asyncio.run(exercise())


def test_model_list_keeps_event_loop_responsive_during_slow_registry_read(tmp_path):
    import time
    class SlowModels(Engines):
        def list_models(self):
            time.sleep(.15)
            return super().list_models()

        def runtime_status(self):
            time.sleep(.15)
            return {'available': True}

    async def exercise():
        service = RagService(CoreRepository(tmp_path / 'agent.db'), SlowModels())
        listing = asyncio.create_task(service.handle('model.list', {}))
        await asyncio.sleep(.02)
        assert not listing.done()
        assert (await listing)['runtime']['available']
        await service.close()
    asyncio.run(exercise())


def test_import_text_file_path_creates_revisions_of_same_document(tmp_path):
    async def exercise():
        service = RagService(CoreRepository(tmp_path / 'agent.db'), Engines())
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        path = tmp_path / 'standard.md'
        path.write_text('25.1309 第一版安全性', encoding='utf-8')
        imported = await wait_job(service, await service.handle('document.import', {'library_id': library, 'paths': [str(path)], 'decisions': {str(path): 'replace'}}))
        assert imported['status'] == 'completed', imported
        first = service.repository.list_libraries()[0]['documents'][0]
        path.write_text('25.1309 第二版安全性', encoding='utf-8')
        assert (await wait_job(service, await service.handle('document.import', {'library_id': library, 'paths': [str(path)], 'decisions': {str(path): 'replace'}})))['status'] == 'completed'
        documents = service.repository.list_libraries()[0]['documents']
        assert len(documents) == 2
        assert {document['id'] for document in documents} == {first['id']}
        assert len({document['version_id'] for document in documents}) == 2
        await service.close()
    asyncio.run(exercise())


def test_withdrawing_unbound_version_preserves_project_generation(tmp_path):
    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        project = core.create_project('Project', 'work')['id']
        service = RagService(core, Engines())
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
        first = await ready(service, library)
        assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
        before = service.project_state(project)
        unrelated = await ready(service, library, 'Additional', '25.1 新资料')
        await service.handle('document.withdraw', {'version_id': unrelated})
        assert service.project_state(project) == before
        assert (await service.search(project, '失效'))['evidence'][0]['version_id'] == first
        await service.close()
    asyncio.run(exercise())


def test_batch_import_gives_each_parser_its_budget_after_queue_wait(tmp_path, monkeypatch):
    from agent.document_parser import ParserService
    from agent.rag.documents import parse_document
    import agent.rag.service as service_module
    monkeypatch.setattr(service_module, 'DOCUMENT_PARSE_TIMEOUT', .3)
    async def exercise():
        service = RagService(CoreRepository(tmp_path / 'agent.db'), Engines())
        # Keep the real parser semaphore/timeout; replace only subprocess work.
        assert isinstance(service.parser, ParserService)
        service.parser.timeout = .3
        async def delayed_execute(original, stage, request_id):
            await asyncio.sleep(.2)
            return parse_document(original)
        monkeypatch.setattr(service.parser, '_execute', delayed_execute)
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        paths = [tmp_path / 'first.txt', tmp_path / 'second.txt']
        for index, path in enumerate(paths):
            path.write_text(f'25.1309 Safety requirement {index}', encoding='utf-8')
        try:
            result = await service.handle('document.import', {'library_id': library, 'paths': [str(path) for path in paths]})
            jobs = [await wait_job(service, {'job': job}) for job in result['jobs']]
            assert [job['status'] for job in jobs] == ['completed'], jobs
            documents = service.repository.list_libraries()[0]['documents']
            assert {document['title'] for document in documents} == {'first', 'second'}
            assert len(documents) == 2
            assert list(service.parser.staging.iterdir()) == []
        finally:
            await service.close()
    asyncio.run(exercise())


def test_timed_out_document_import_cannot_publish_after_worker_timeout(tmp_path):
    import sys
    from agent.document_parser import ParserService
    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        parser = ParserService(tmp_path / 'rag', command=[sys.executable, '-c', 'import time;time.sleep(30)'], timeout=.1)
        service = RagService(core, Engines(), parser=parser)
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        path = tmp_path / 'slow.txt'
        path.write_text('25.1309 安全性', encoding='utf-8')
        result = await wait_job(service, await service.handle('document.import', {'library_id': library, 'paths': [str(path)]}))
        assert result['status'] == 'failed'
        assert 'time limit' in result['error']
        await asyncio.sleep(.12)
        assert service.repository.list_libraries()[0]['documents'] == []
        assert service.repository.job(result['id'])['status'] == 'failed'
        assert list(parser.staging.iterdir()) == []
        await service.close()
    asyncio.run(exercise())


def test_original_copy_timeout_cannot_publish_a_late_result(tmp_path, monkeypatch):
    import time
    import agent.rag.service as service_module
    monkeypatch.setattr(service_module, 'DOCUMENT_PARSE_TIMEOUT', .01)

    async def exercise():
        service = RagService(CoreRepository(tmp_path / 'agent.db'), Engines())
        library = (await service.handle('library.create', {'name': 'Library', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        extract = service._extract_original
        def delayed_extract(path):
            time.sleep(.08)
            return extract(path)
        monkeypatch.setattr(service, '_extract_original', delayed_extract)
        path = tmp_path / 'slow-copy.txt'
        path.write_text('25.1309 Safety', encoding='utf-8')
        try:
            job = await wait_job(service, await service.handle('document.import', {'library_id': library, 'paths': [str(path)]}))
            assert job['status'] == 'failed'
            assert 'time limit' in job['error']
            await asyncio.sleep(.12)
            assert service.repository.list_libraries()[0]['documents'] == []
            assert service.repository.job(job['id'])['status'] == 'failed'
            assert list(service.parser.staging.iterdir()) == []
        finally:
            await service.close()
    asyncio.run(exercise())


def test_cancelled_import_rejects_result_from_parser_that_suppresses_cancel(tmp_path):
    class DelayedParser:
        def __init__(self):
            self.entered = asyncio.Event()

        async def parse(self, path):
            from agent.rag.documents import parse_document
            self.entered.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return parse_document(path)

        async def close(self):
            pass

    async def exercise():
        parser = DelayedParser()
        service = RagService(CoreRepository(tmp_path / 'agent.db'), Engines(), parser=parser)
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        path = tmp_path / 'standard.txt'
        path.write_text('25.1309 Safety', encoding='utf-8')
        job = (await service.handle('document.import', {'library_id': library, 'paths': [str(path)]}))['job']
        await parser.entered.wait()
        await service.handle('job.cancel', {'job_id': job['id']})
        await asyncio.sleep(.05)
        assert service.repository.job(job['id'])['status'] == 'cancelled'
        assert service.repository.list_libraries()[0]['documents'] == []
        await service.close()
    asyncio.run(exercise())


def test_cancelled_paste_rejects_result_from_parser_that_suppresses_cancel(tmp_path):
    class DelayedParser:
        def __init__(self):
            self.entered = asyncio.Event()

        async def parse(self, path):
            from agent.rag.documents import parse_document
            self.entered.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return parse_document(path)

        async def close(self):
            pass

    async def exercise():
        parser = DelayedParser()
        service = RagService(CoreRepository(tmp_path / 'agent.db'), Engines(), parser=parser)
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        task = asyncio.create_task(service.handle('document.paste', {
            'library_id': library, 'title': 'Cancelled', 'content': '25.1309 Safety'}))
        await parser.entered.wait()
        task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        try:
            assert service.repository.list_libraries()[0]['documents'] == []
            assert isinstance(results[0], asyncio.CancelledError)
            assert service.repository.jobs() == []
        finally:
            await service.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('binding_changes', [False, True], ids=['valid_guard', 'stale_guard'])
def test_complete_model_run_checks_rag_generation_before_publishing(tmp_path, binding_changes):
    from contextlib import closing
    from agent.rag.repository import RagRepository

    core = CoreRepository(tmp_path / 'agent.db')
    project = core.create_project('Project', 'work')['id']
    repository = RagRepository(core)
    library = repository.create_library({'name': 'Regulation', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'})
    repository.bind({'library_id': library['id'], 'project_id': project, 'enabled': True})
    run = core.start_model_run(content='Evaluate the requirement', idempotency_key='rag-commit-guard',
                               provider_profile_id='builtin-demo', model_id='demo', kind='project_work', project_id=project)
    attempt = core.begin_run_attempt(run['id'])
    core.transition_model_run(run['id'], {'queued'}, 'generating')
    core.checkpoint_draft(run['id'], attempt['id'], 'Draft awaiting evidence validation')
    finalized_guard = (project, repository.state(project)['generation'])

    if binding_changes:
        repository.bind({'library_id': library['id'], 'project_id': project, 'enabled': False})
        with pytest.raises(CoreDataError) as error:
            core.complete_model_run(run['id'], attempt['id'], 'Final answer', rag_guard=finalized_guard)
        assert error.value.code == 'rag_generation_changed'
        persisted = core.get_model_run(run['id'])
        assert persisted['status'] == 'generating'
        assert persisted['assistant_message_id'] is None
        assert persisted['finished_at'] is None
        assert not any(message['role'] == 'assistant' for message in core.get_conversation(run['conversation_id'])['messages'])
    else:
        completed = core.complete_model_run(run['id'], attempt['id'], 'Final answer', rag_guard=finalized_guard)
        assert completed['status'] == 'completed'
        assert completed['assistant_message']['content'] == 'Final answer'
        assert completed['assistant_message_id'] == completed['assistant_message']['id']
        assert completed['finished_at'] is not None

    with closing(core._connect()) as connection:
        persisted_attempt = connection.execute('SELECT finished_at FROM run_attempts WHERE id=?', (attempt['id'],)).fetchone()
        draft = connection.execute('SELECT content FROM assistant_drafts WHERE run_id=?', (run['id'],)).fetchone()
    if binding_changes:
        assert persisted_attempt['finished_at'] is None
        assert draft['content'] == 'Draft awaiting evidence validation'
    else:
        assert persisted_attempt['finished_at'] is not None
        assert draft is None


def test_parent_context_read_preserves_fragment_identity_and_complete_exception(tmp_path):
    async def exercise():
        import hashlib
        core = CoreRepository(tmp_path / 'agent.db')
        project = core.create_project('Project', 'work')['id']
        service = RagService(core, Engines())
        library = (await service.handle('library.create', {'name': '法规', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
        await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
        exception = '除非经局方批准，否则不得省略独立的验证试验。'
        content = '25.1309 ' + '失效状态应保持安全。' * 130 + exception
        await ready(service, library, content=content)
        assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
        evidence = (await service.search(project, '失效', run_id='context-run'))['evidence'][0]
        with service.repository.connect() as connection:
            original = connection.execute('SELECT original_hash FROM rag_versions WHERE id=?', (evidence['version_id'],)).fetchone()[0]
        assert evidence['original_hash'] == original
        read = service.read_evidence(project, evidence['id'])
        assert read['text'] == evidence['text']
        assert read['text_hash'] == evidence['text_hash']
        context = read['parent_context']
        assert context['text'] == content
        assert exception in context['text']
        assert context['text_hash'] == hashlib.sha256(content.encode()).hexdigest()
        assert not context['has_more']
        first_page = service.read_evidence(project, evidence['id'], length=50)['parent_context']
        second_page = service.read_evidence(project, evidence['id'], offset=50, length=50)['parent_context']
        assert first_page['text'] + second_page['text'] == content[:100]
        assert first_page['has_more'] and second_page['has_more']
        assert second_page['text_hash'] == hashlib.sha256(content[50:100].encode()).hexdigest()
        assert first_page['total_length'] == len(content)
        from contextlib import closing
        with closing(service.repository.connect()) as connection:
            versions = connection.execute('SELECT splitter_versions FROM rag_indexes').fetchone()[0]
        import json
        assert json.loads(versions) == ['source-block-v4']
        await service.close()
    asyncio.run(exercise())
