import zipfile
import pytest
from agent.rag.documents import NS
from agent.storage import CoreDataError


def test_docx_stream_yields_before_malformed_tail(tmp_path):
    from agent.document_parser.incremental import stream_document
    path = tmp_path / 'source.docx'
    with zipfile.ZipFile(path, 'w') as archive:
        paragraphs = ''.join('<w:p><w:r><w:t>Paragraph %s</w:t></w:r></w:p>' % i for i in range(2000))
        archive.writestr('word/document.xml', '<w:document xmlns:w="%s"><w:body>%s<broken' % (NS[1:-1], paragraphs))
    stream = stream_document(path)
    first = next(stream['chunks'])
    assert first['text'] == 'Paragraph 0'
    assert first['locator']['paragraph'] == 1
    with pytest.raises(CoreDataError):
        list(stream['chunks'])


def test_stream_docx_rows_notes_and_stable_blocks(tmp_path):
    from agent.document_parser.incremental import stream_document
    path = tmp_path / 'source.docx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="%s"><w:body><w:p><w:r><w:t>25.1309 Safety</w:t></w:r><w:footnoteReference w:id="1"/></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>' % NS[1:-1])
        archive.writestr('word/footnotes.xml', '<w:footnotes xmlns:w="%s"><w:footnote w:id="1"><w:p><w:r><w:t>Note</w:t></w:r></w:p></w:footnote></w:footnotes>' % NS[1:-1])
    result = stream_document(path)
    chunks = list(result['chunks'])
    assert [c['text'] for c in chunks] == ['25.1309 Safety', 'Cell', 'Note']
    assert chunks[1]['locator']['table'] == 1
    assert chunks[2]['locator']['clause_parent_id'] == 'block-1'

def test_v2_result_sequence_source_and_incomplete_prefix(tmp_path):
    from agent.document_parser.protocol import write_result, read_result, read_partial_result
    from agent.rag.documents import parse_text
    parsed = parse_text('25.1309 Safety\n\nExceptions apply.')
    for chunk in parsed['chunks']:
        chunk['locator']['kind'] = 'docx'
    parsed.update(source_sha256='a' * 64, protocol_version=2)
    output = tmp_path / 'result.ndjson'
    write_result(output, 'r' * 32, parsed, 100000)
    frames = output.read_bytes().splitlines(keepends=True)
    output.write_bytes(b''.join(frames[:-1]))
    partial = read_partial_result(output, 'r' * 32, 100000, 'a' * 64)
    assert partial['complete'] is False
    assert partial['sequence'] == 2
    with pytest.raises(CoreDataError):
        read_result(output, 'r' * 32, 100000)
    output.write_bytes(b''.join(frames))
    assert read_result(output, 'r' * 32, 100000)['chunks'] == parsed['chunks']
    with pytest.raises(CoreDataError):
        read_partial_result(output, 'r' * 32, 100000, 'b' * 64)
    output.write_bytes(b''.join([frames[0], frames[2], frames[1], frames[3]]))
    with pytest.raises(CoreDataError):
        read_partial_result(output, 'r' * 32, 100000, 'a' * 64)


def test_service_emits_verified_partials_before_parse_error(tmp_path):
    import asyncio
    import hashlib
    import io
    from agent.document_parser import ParserService
    async def scenario():
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as archive:
            paragraphs = ''.join('<w:p><w:r><w:t>Paragraph %s</w:t></w:r></w:p>' % i for i in range(2000))
            archive.writestr('word/document.xml', '<w:document xmlns:w="%s"><w:body>%s<broken' % (NS[1:-1], paragraphs))
        digest = hashlib.sha256(content.getvalue()).hexdigest()
        source = tmp_path / 'originals' / digest[:2] / (digest + '.docx')
        source.parent.mkdir(parents=True)
        source.write_bytes(content.getvalue())
        partials = []
        parser = ParserService(tmp_path)
        try:
            with pytest.raises(CoreDataError):
                await parser.parse(source, on_partial=partials.append)
            assert partials
            assert all(item['complete'] is False and item['source_sha256'] == digest for item in partials)
            assert [item['sequence'] for item in partials] == list(range(1, len(partials) + 1))
        finally:
            await parser.close()
    asyncio.run(scenario())

def test_attachment_read_returns_before_extraction_completes_and_resumes_index(tmp_path, monkeypatch):
    import asyncio
    from agent.document_parser import ParserService
    from agent.document_attachments import DocumentAttachments
    from agent.storage import CoreRepository
    from test_document_attachments import source_attachment
    from agent.rag.documents import _chunks
    async def scenario():
        core = CoreRepository(tmp_path / 'agent.db')
        parser = ParserService(tmp_path / 'rag')
        release = asyncio.Event()
        parsed = _chunks([('First body', {'kind': 'docx', 'paragraph': 1}),
                          ('Second body', {'kind': 'docx', 'paragraph': 2})], [])
        async def parse(path, *, on_partial=None):
            await on_partial({'chunks': [parsed['chunks'][0]], 'sequence': 1})
            await release.wait()
            await on_partial({'chunks': [parsed['chunks'][1]], 'sequence': 2})
            return parsed
        monkeypatch.setattr(parser, 'parse', parse)
        service = DocumentAttachments(core, parser=parser)
        monkeypatch.setattr(service, '_read_identity', lambda: 'verified-parser-test')
        attachment = source_attachment(tmp_path)
        first = await asyncio.wait_for(service.read(attachment, limit=1, include_coverage=True), 5)
        assert first['extraction_state'] == 'partial'
        assert first['coverage_complete'] is False
        assert first['_coverage']['complete'] is False
        assert first['blocks'][0]['text'] == 'First body'
        release.set()
        second = await asyncio.wait_for(service.read(attachment, cursor=first['next_cursor'], limit=1), 5)
        assert second['blocks'][0]['text'] == 'Second body'
        await service._incremental_reads[next(iter(service._incremental_reads))]['task']
        completed = await service.read(attachment)
        assert completed['coverage_complete'] is True
        stable = completed['blocks']
        await service.close()
        restarted = DocumentAttachments(core, parser=parser)
        monkeypatch.setattr(restarted, '_read_identity', lambda: 'verified-parser-test')
        resumed = await restarted.read(attachment)
        assert resumed['blocks'] == stable
        assert resumed['coverage_complete'] is True
        await restarted.close()
        await parser.close()
    asyncio.run(scenario())

def test_v2_partial_reader_accepts_only_whole_verified_frames(tmp_path):
    from agent.document_parser.protocol import write_result, read_partial_result
    from agent.rag.documents import _chunks
    parsed = _chunks([('Body', {'kind': 'docx', 'paragraph': 1})], [])
    parsed.update(source_sha256='a' * 64, protocol_version=2)
    path = tmp_path / 'result.ndjson'
    write_result(path, 'r' * 32, parsed, 100000)
    frames = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(frames[0] + frames[1] + frames[2][:10])
    result = read_partial_result(path, 'r' * 32, 100000, 'a' * 64)
    assert result['complete'] is False
    assert result['sequence'] == 1

def test_service_partial_chunks_match_verified_completion(tmp_path):
    import asyncio
    import hashlib
    from agent.document_parser import ParserService
    async def scenario():
        content = b'25.1309 Safety\n\nExceptions apply.'
        digest = hashlib.sha256(content).hexdigest()
        source = tmp_path / 'originals' / digest[:2] / (digest + '.txt')
        source.parent.mkdir(parents=True)
        source.write_bytes(content)
        partials = []
        async def partial(frame):
            partials.extend(frame['chunks'])
        parser = ParserService(tmp_path)
        try:
            result = await parser.parse(source, on_partial=partial)
            assert partials == result['chunks']
        finally:
            await parser.close()
    asyncio.run(scenario())

def test_background_attachment_parse_does_not_hold_foreground_execution_stage(tmp_path, monkeypatch):
    import asyncio
    from agent.document_parser import ParserService
    from agent.document_attachments import DocumentAttachments
    from agent.execution.activity import run_stage
    from agent.execution.execution_wait import ExecutionMonitor
    from agent.storage import CoreRepository
    from test_document_attachments import source_attachment
    from agent.rag.documents import _chunks
    async def scenario():
        core = CoreRepository(tmp_path / 'agent.db')
        monitor = ExecutionMonitor(core)
        root = monitor.start_task(run_id='parallel-read')
        parser = ParserService(tmp_path / 'rag')
        release = asyncio.Event()
        parsed = _chunks([('First body', {'kind': 'docx', 'paragraph': 1})], [])
        async def parse(path, *, on_partial=None):
            async def work(activity):
                await on_partial({'chunks': parsed['chunks'], 'sequence': 1})
                await release.wait()
                return parsed
            return await run_stage('document_parsing', work)
        monkeypatch.setattr(parser, 'parse', parse)
        service = DocumentAttachments(core, parser=parser)
        monkeypatch.setattr(service, '_read_identity', lambda: 'verified-parser-test')
        try:
            with monitor.associate(root['id']):
                first = await asyncio.wait_for(service.read(source_attachment(tmp_path), limit=1), 5)
                assert first['extraction_state'] == 'partial'
                async def foreground(activity):
                    return 'foreground-complete'
                assert await run_stage('model_output', foreground) == 'foreground-complete'
                release.set()
                await service._incremental_reads[next(iter(service._incremental_reads))]['task']
        finally:
            release.set()
            await service.close()
            await parser.close()
    asyncio.run(scenario())

def test_cancelled_background_read_can_restart_under_new_execution_root(tmp_path, monkeypatch):
    import asyncio
    from agent.document_parser import ParserService
    from agent.document_attachments import DocumentAttachments
    from agent.execution.execution_wait import ExecutionMonitor
    from agent.storage import CoreRepository
    from test_document_attachments import source_attachment
    from agent.rag.documents import _chunks
    async def scenario():
        core = CoreRepository(tmp_path / 'agent.db')
        monitor = ExecutionMonitor(core)
        root = monitor.start_task(run_id='cancelled-read')
        parser = ParserService(tmp_path / 'rag')
        release = asyncio.Event()
        parsed = _chunks([('First body', {'kind': 'docx', 'paragraph': 1})], [])
        async def parse(path, *, on_partial=None):
            await on_partial({'chunks': parsed['chunks'], 'sequence': 1})
            await release.wait()
            return parsed
        monkeypatch.setattr(parser, 'parse', parse)
        service = DocumentAttachments(core, parser=parser)
        monkeypatch.setattr(service, '_read_identity', lambda: 'verified-parser-test')
        attachment = source_attachment(tmp_path)
        try:
            with monitor.associate(root['id']):
                await service.read(attachment, limit=1)
            state = next(iter(service._incremental_reads.values()))
            task = state['task']
            monitor.finish_task(root['id'], 'cancelled')
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert state['index'].get('state') == 'error'
            retried = monitor.start_task(run_id='retried-read')
            with monitor.associate(retried['id']):
                await service.read(attachment, limit=1)
                release.set()
                await asyncio.wait_for(state['task'], 5)
                result = await service.read(attachment, limit=1)
                assert result['coverage_complete'] is True
        finally:
            release.set()
            await service.close()
            await parser.close()
    asyncio.run(scenario())

def test_shared_background_read_survives_one_owner_cancellation(tmp_path, monkeypatch):
    import asyncio
    from agent.document_parser import ParserService
    from agent.document_attachments import DocumentAttachments
    from agent.execution.execution_wait import ExecutionMonitor
    from agent.storage import CoreRepository
    from test_document_attachments import source_attachment
    from agent.rag.documents import _chunks
    async def scenario():
        core = CoreRepository(tmp_path / 'agent.db')
        monitor = ExecutionMonitor(core)
        first_root = monitor.start_task(run_id='first-reader')
        second_root = monitor.start_task(run_id='second-reader')
        parser = ParserService(tmp_path / 'rag')
        release = asyncio.Event()
        parsed = _chunks([('First body', {'kind': 'docx', 'paragraph': 1})], [])
        async def parse(path, *, on_partial=None):
            await on_partial({'chunks': parsed['chunks'], 'sequence': 1})
            await release.wait()
            return parsed
        monkeypatch.setattr(parser, 'parse', parse)
        service = DocumentAttachments(core, parser=parser)
        monkeypatch.setattr(service, '_read_identity', lambda: 'verified-parser-test')
        attachment = source_attachment(tmp_path)
        try:
            with monitor.associate(first_root['id']):
                await service.read(attachment, limit=1)
            with monitor.associate(second_root['id']):
                await service.read(attachment, limit=1)
            task = next(iter(service._incremental_reads.values()))['task']
            monitor.finish_task(first_root['id'], 'cancelled')
            await asyncio.sleep(.15)
            assert not task.done()
            release.set()
            await task
        finally:
            release.set()
            await service.close()
            await parser.close()
    asyncio.run(scenario())

def test_default_read_waits_for_requested_page_or_extraction_completion(tmp_path, monkeypatch):
    import asyncio
    from agent.document_parser import ParserService
    from agent.document_attachments import DocumentAttachments
    from agent.storage import CoreRepository
    from test_document_attachments import source_attachment
    from agent.rag.documents import _chunks
    async def scenario():
        core = CoreRepository(tmp_path / 'agent.db')
        parser = ParserService(tmp_path / 'rag')
        emitted = asyncio.Event()
        release = asyncio.Event()
        parsed = _chunks([('Short body', {'kind': 'docx', 'paragraph': 1})], [])
        async def parse(path, *, on_partial=None):
            await on_partial({'chunks': parsed['chunks'], 'sequence': 1})
            emitted.set()
            await release.wait()
            return parsed
        monkeypatch.setattr(parser, 'parse', parse)
        service = DocumentAttachments(core, parser=parser)
        monkeypatch.setattr(service, '_read_identity', lambda: 'verified-parser-test')
        reading = asyncio.create_task(service.read(source_attachment(tmp_path)))
        try:
            await asyncio.wait_for(emitted.wait(), 5)
            await asyncio.sleep(0)
            assert not reading.done()
            release.set()
            result = await asyncio.wait_for(reading, 5)
            assert result['coverage_complete'] is True
            assert result['extraction_state'] == 'complete'
            assert result['next_cursor'] is None
        finally:
            release.set()
            await asyncio.gather(reading, return_exceptions=True)
            await service.close()
            await parser.close()
    asyncio.run(scenario())
