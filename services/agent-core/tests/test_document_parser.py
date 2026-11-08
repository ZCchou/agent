import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import time
import zipfile

import pytest

from agent.storage import CoreDataError


def managed(data_dir, content=b'25.1309 Safety\n\nExceptions apply.', suffix='.txt'):
    digest = hashlib.sha256(content).hexdigest()
    path = data_dir / 'originals' / digest[:2] / (digest + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_source_worker_preserves_unicode_text_positions_and_cleans_staging(tmp_path):
    async def exercise():
        from agent.document_parser import ParserService
        data = tmp_path / '法规资料'
        original = managed(data, '25.1309 安全性\n\n除非批准。'.encode())
        parser = ParserService(data)
        try:
            result = await parser.parse(original)
            assert [chunk['text'] for chunk in result['chunks']] == ['25.1309 安全性', '除非批准。']
            assert result['chunks'][1]['locator']['line_start'] == 3
            assert result['chunks'][1]['locator']['clause_parent_id'] == 'block-1'
            assert result['chunks'][0]['text_hash'] == hashlib.sha256('25.1309 安全性'.encode()).hexdigest()
            assert list((data / 'parser-staging').iterdir()) == []
        finally:
            await parser.close()
    asyncio.run(exercise())


def test_only_repository_originals_are_accepted(tmp_path):
    async def exercise():
        from agent.document_parser import ParserService
        parser = ParserService(tmp_path / 'rag')
        outside = tmp_path / 'outside.txt'
        outside.write_text('content')
        arbitrary = tmp_path / 'rag' / 'originals' / 'arbitrary.txt'
        arbitrary.parent.mkdir(parents=True)
        arbitrary.write_text('content')
        for path in [outside, arbitrary]:
            with pytest.raises(CoreDataError) as error:
                await parser.parse(path)
            assert error.value.code == 'rag_document_unavailable'
        await parser.close()
    asyncio.run(exercise())


def test_docx_worker_keeps_notes_and_warnings(tmp_path):
    async def exercise():
        from agent.document_parser import ParserService
        import io
        content = io.BytesIO()
        ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
        with zipfile.ZipFile(content, 'w') as archive:
            archive.writestr('word/document.xml', f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r><w:t>25.1309 Safety</w:t></w:r><w:footnoteReference w:id="1"/></w:p></w:body></w:document>')
            archive.writestr('word/footnotes.xml', f'<w:footnotes xmlns:w="{ns}"><w:footnote w:id="1"><w:p><w:r><w:t>Exception</w:t></w:r></w:p></w:footnote></w:footnotes>')
        parser = ParserService(tmp_path)
        result = await parser.parse(managed(tmp_path, content.getvalue(), '.docx'))
        assert result['chunks'][1]['locator']['part'] == 'footnote'
        assert result['chunks'][1]['locator']['clause_parent_id'] == 'block-1'
        assert result['warnings']
        await parser.close()
    asyncio.run(exercise())


def test_worker_timeout_and_crash_leave_service_reusable(tmp_path):
    async def exercise():
        from agent.document_parser import ParserService
        original = managed(tmp_path)
        for command, timeout, code in [([sys.executable, '-c', 'import time;time.sleep(30)'], .1, 'rag_document_timeout'),
                                       ([sys.executable, '-c', 'raise SystemExit(4)'], 5, 'rag_parser_invalid_result')]:
            parser = ParserService(tmp_path, command=command, timeout=timeout)
            with pytest.raises(CoreDataError) as error:
                await parser.parse(original)
            assert error.value.code == code
            assert list((tmp_path / 'parser-staging').iterdir()) == []
            await parser.close()
        parser = ParserService(tmp_path)
        assert (await parser.parse(original))['chunks']
        await parser.close()
    asyncio.run(exercise())


def test_close_interrupts_active_and_queued_requests(tmp_path):
    async def exercise():
        from agent.document_parser import ParserService
        parser = ParserService(tmp_path, command=[sys.executable, '-c', 'import time;time.sleep(30)'])
        original = managed(tmp_path)
        active = asyncio.create_task(parser.parse(original))
        await asyncio.sleep(.1)
        queued = asyncio.create_task(parser.parse(original))
        await asyncio.sleep(.05)
        await asyncio.wait_for(parser.close(), 5)
        results = await asyncio.gather(active, queued, return_exceptions=True)
        assert all(isinstance(item, asyncio.CancelledError) for item in results)
        assert list((tmp_path / 'parser-staging').iterdir()) == []
        with pytest.raises(CoreDataError):
            await parser.parse(original)
    asyncio.run(exercise())


def test_text_pdf_and_markdown_use_actual_source_worker(tmp_path):
    from test_rag_documents import make_pdf
    async def exercise():
        from agent.document_parser import ParserService
        pdf = tmp_path / 'source.pdf'
        make_pdf(pdf, '25.1309 System safety')
        parser = ParserService(tmp_path)
        parsed = await parser.parse(managed(tmp_path, pdf.read_bytes(), '.pdf'))
        assert parsed['chunks'][0]['text'] == '25.1309 System safety'
        assert parsed['chunks'][0]['locator']['page'] == 1
        assert parsed['warnings']
        markdown = await parser.parse(managed(tmp_path, b'# Heading\n\n25.1309 Safety', '.md'))
        assert markdown['chunks'][1]['locator']['kind'] == 'markdown'
        assert markdown['chunks'][1]['locator']['line_start'] == 3
        await parser.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('failure', ['hash', 'truncated', 'oversize', 'status', 'wrong_request', 'structure'])
def test_invalid_worker_outputs_are_rejected_and_staging_removed(tmp_path, failure):
    async def exercise():
        from agent.document_parser import ParserService
        script = '''import sys,json,pathlib,hashlib
r=json.loads(sys.stdin.buffer.readline())
c={'text':'abc','text_hash':hashlib.sha256(b'abc').hexdigest(),'clause_number':None,'locator':{'kind':'text','block_id':'block-1'}}
frames=[{'type':'header','request_id':r['request_id'],'warnings':[]},{'type':'chunk','chunk':c},{'type':'end','request_id':r['request_id'],'chunks':1}]
mode=sys.argv[1]
if mode=='hash': c['text_hash']='0'*64
if mode=='structure': c['locator']=[]
if mode=='wrong_request': frames[0]['request_id']='0'*32
if mode=='truncated': frames.pop()
data=''.join(json.dumps(f)+'\\n' for f in frames)
if mode=='oversize': data='x'*2049
pathlib.Path(r['output_path']).write_text(data,encoding='utf-8')
if mode=='status': sys.stdout.write('x'*(1024*1024+1))
else: print(json.dumps({'request_id':r['request_id'],'status':'complete'}))
'''
        parser = ParserService(tmp_path, command=[sys.executable, '-c', script, failure], result_limit=2048)
        with pytest.raises(CoreDataError) as error:
            await parser.parse(managed(tmp_path))
        assert error.value.code == 'rag_parser_invalid_result'
        assert list((tmp_path / 'parser-staging').iterdir()) == []
        await parser.close()
    asyncio.run(exercise())


def test_actual_worker_respects_output_byte_limit(tmp_path):
    async def exercise():
        from agent.document_parser import ParserService
        parser = ParserService(tmp_path, result_limit=200)
        with pytest.raises(CoreDataError):
            await parser.parse(managed(tmp_path))
        assert list((tmp_path / 'parser-staging').iterdir()) == []
        await parser.close()
    asyncio.run(exercise())


def test_restart_only_removes_marked_owned_flat_staging(tmp_path):
    from agent.document_parser import ParserService
    root = tmp_path / 'parser-staging'
    owned = root / ('parser-' + 'a' * 32)
    owned.mkdir(parents=True)
    (owned / '.agent-parser-owned').write_text('agent-document-parser-v1\n' + owned.name + '\n')
    (owned / 'result.ndjson').write_text('incomplete')
    unrelated = root / 'user-files'
    unrelated.mkdir()
    (unrelated / 'important.txt').write_text('keep')
    unknown = root / ('parser-' + 'b' * 32)
    unknown.mkdir()
    (unknown / '.agent-parser-owned').write_text('user-owned')
    parser = ParserService(tmp_path)
    assert not owned.exists()
    assert (unrelated / 'important.txt').read_text() == 'keep'
    assert unknown.exists()
    asyncio.run(parser.close())


def test_restart_preserves_non_ascii_ownership_marker_and_remains_usable(tmp_path):
    async def exercise():
        from agent.document_parser import ParserService
        candidate = tmp_path / 'parser-staging' / ('parser-' + 'c' * 32)
        candidate.mkdir(parents=True)
        marker = candidate / '.agent-parser-owned'
        marker.write_bytes(b'\xff')
        user_file = candidate / 'result.ndjson'
        user_file.write_bytes(b'user content')
        parser = ParserService(tmp_path)
        try:
            assert marker.read_bytes() == b'\xff'
            assert user_file.read_bytes() == b'user content'
            assert (await parser.parse(managed(tmp_path)))['chunks'][0]['text'] == '25.1309 Safety'
        finally:
            await parser.close()
    asyncio.run(exercise())


def test_hardlinked_original_is_rejected(tmp_path):
    async def exercise():
        from agent.document_parser import ParserService
        original = managed(tmp_path)
        os.link(original, tmp_path / 'other.txt')
        parser = ParserService(tmp_path)
        with pytest.raises(CoreDataError):
            await parser.parse(original)
        await parser.close()
    asyncio.run(exercise())


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job enforcement')
def test_real_job_memory_limit_fails_without_large_host_allocation(tmp_path):
    async def exercise():
        from agent.document_parser import ParserService
        script = 'import sys;sys.stdin.buffer.readline();x=bytearray(256*1024*1024)'
        parser = ParserService(tmp_path, command=[sys.executable, '-c', script],
                               memory_limit=96 * 1024 * 1024, timeout=10)
        with pytest.raises(CoreDataError):
            await parser.parse(managed(tmp_path))
        assert list((tmp_path / 'parser-staging').iterdir()) == []
        await parser.close()
    asyncio.run(exercise())


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction enforcement')
def test_junction_staging_and_originals_are_rejected_without_deleting_target(tmp_path):
    from agent.document_parser import ParserService
    data = tmp_path / 'rag'
    data.mkdir()
    target = tmp_path / 'user-folder'
    target.mkdir()
    important = target / 'important.txt'
    important.write_text('keep')
    junction = data / 'parser-staging'
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target)], check=True, capture_output=True)
    try:
        with pytest.raises(CoreDataError):
            ParserService(data)
        assert important.read_text() == 'keep'
    finally:
        junction.rmdir()
    original = managed(data)
    actual = data / 'originals'
    renamed = data / 'saved-originals'
    actual.rename(renamed)
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(actual), str(renamed)], check=True, capture_output=True)
    async def exercise():
        parser = ParserService(data)
        with pytest.raises(CoreDataError):
            await parser.parse(original)
        await parser.close()
    try:
        asyncio.run(exercise())
    finally:
        actual.rmdir()


def test_frozen_mode_disallows_command_and_limit_overrides(tmp_path, monkeypatch):
    from agent.document_parser import ParserService
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    for overrides in [{'command': ['other.exe']}, {'memory_limit': 1}, {'timeout': 1}, {'concurrency': 2}, {'result_limit': 10}]:
        with pytest.raises(ValueError):
            ParserService(tmp_path, **overrides)


def test_core_document_worker_switch_runs_single_request_without_core_setup(tmp_path):
    async def exercise():
        from agent.document_parser import ParserService
        parser = ParserService(tmp_path, command=[sys.executable, '-m', 'agent.main', '--document-worker'])
        assert (await parser.parse(managed(tmp_path)))['chunks'][0]['text'] == '25.1309 Safety'
        assert not (tmp_path / 'agent.db').exists()
        await parser.close()
    asyncio.run(exercise())


def test_repeated_document_failures_do_not_poison_parser(tmp_path):
    async def exercise():
        from agent.document_parser import ParserService
        parser = ParserService(tmp_path)
        for _ in range(2):
            with pytest.raises(CoreDataError) as error:
                await parser.parse(managed(tmp_path, b'  '))
            assert error.value.code == 'rag_document_no_text'
        assert (await parser.parse(managed(tmp_path)))['chunks'][0]['text'] == '25.1309 Safety'
        await parser.close()
    asyncio.run(exercise())


def test_cancelling_queue_does_not_interrupt_active_request(tmp_path):
    async def exercise():
        from agent.document_parser import ParserService
        script = 'import time;time.sleep(.3);from agent.document_parser.worker import main;raise SystemExit(main())'
        parser = ParserService(tmp_path, command=[sys.executable, '-c', script])
        original = managed(tmp_path)
        first = asyncio.create_task(parser.parse(original))
        await asyncio.sleep(.02)
        queued = asyncio.create_task(parser.parse(original))
        await asyncio.sleep(.02)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert (await first)['chunks'][0]['text'] == '25.1309 Safety'
        await parser.close()
    asyncio.run(exercise())


def test_close_awaits_reader_after_request_was_already_cancelled(tmp_path, monkeypatch):
    import threading
    async def exercise():
        from agent.document_parser import ParserService
        entered, finished = threading.Event(), threading.Event()
        exchange = ParserService._exchange
        def delayed_reader(process, request):
            entered.set()
            try:
                return exchange(process, request)
            finally:
                time.sleep(.2)
                finished.set()
        monkeypatch.setattr(ParserService, '_exchange', staticmethod(delayed_reader))
        parser = ParserService(tmp_path, command=[sys.executable, '-c', 'import time;time.sleep(30)'])
        task = asyncio.create_task(parser.parse(managed(tmp_path)))
        while not entered.is_set():
            await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(0)
        await parser.close()
        assert finished.is_set(), 'close returned with an outstanding pipe reader'
        assert list((tmp_path / 'parser-staging').iterdir()) == []
    asyncio.run(exercise())


@pytest.mark.parametrize('invalid_frame', ['duplicate_key', 'nonfinite'])
def test_nonstandard_json_status_is_rejected(tmp_path, invalid_frame):
    async def exercise():
        from agent.document_parser import ParserService
        # A valid parse is followed by mutation of only the status transport.
        script = '''import io,sys,json
from agent.document_parser.worker import main
class Capture:
    buffer=io.BytesIO()
original=sys.stdout
sys.stdout=Capture()
code=main()
status=sys.stdout.buffer.getvalue().decode()
sys.stdout=original
if sys.argv[1]=='duplicate_key': status=status.replace('"status":"complete"','"status":"bad","status":"complete"')
else: status=status[:-2]+',"extra":NaN}\\n'
sys.stdout.buffer.write(status.encode())
raise SystemExit(code)
'''
        parser = ParserService(tmp_path, command=[sys.executable, '-c', script, invalid_frame])
        with pytest.raises(CoreDataError):
            await parser.parse(managed(tmp_path))
        await parser.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('field,value', [('kind', 'unknown'), ('offset', -1), ('parent_length', 1),
                                        ('fragment_index', 10), ('continues_after', 'false')])
def test_corrupt_source_positions_are_rejected(tmp_path, field, value):
    async def exercise():
        from agent.document_parser import ParserService
        script = '''import sys,json
from agent.document_parser import worker
write=worker.write_result
def corrupt(path,request_id,parsed,limit):
    parsed['chunks'][0]['locator'][sys.argv[1]]=json.loads(sys.argv[2])
    write(path,request_id,parsed,limit)
worker.write_result=corrupt
raise SystemExit(worker.main())
'''
        parser = ParserService(tmp_path, command=[sys.executable, '-c', script, field, json.dumps(value)])
        with pytest.raises(CoreDataError):
            await parser.parse(managed(tmp_path))
        assert list((tmp_path / 'parser-staging').iterdir()) == []
        await parser.close()
    asyncio.run(exercise())


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job process-tree enforcement')
def test_cancellation_terminates_child_and_grandchild(tmp_path):
    async def exercise():
        from agent.document_parser import ParserService
        from ctypes import WinDLL, byref, c_ulong, c_void_p
        kernel = WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = c_void_p
        kernel.GetExitCodeProcess.argtypes = [c_void_p, c_void_p]
        kernel.CloseHandle.argtypes = [c_void_p]
        pid_file = tmp_path / 'pids.json'
        grandchild_code = 'import time;time.sleep(30)'
        child_code = ('import subprocess,sys,time,json,pathlib,os;'
                      'p=subprocess.Popen([sys.executable,"-c",' + repr(grandchild_code) + ']);'
                      'pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(),p.pid]));time.sleep(30)')
        script = ('import subprocess,sys,time;sys.stdin.buffer.readline();'
                  'subprocess.Popen([sys.executable,"-c",' + repr(child_code) + ',sys.argv[1]]);time.sleep(30)')
        parser = ParserService(tmp_path, command=[sys.executable, '-c', script, str(pid_file)])
        task = asyncio.create_task(parser.parse(managed(tmp_path)))
        try:
            for _ in range(150):
                if pid_file.exists():
                    break
                await asyncio.sleep(.02)
            assert pid_file.exists(), 'Worker descendants did not start'
            pids = json.loads(pid_file.read_text())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            for pid in pids:
                handle = kernel.OpenProcess(0x1000, False, pid)
                if handle:
                    try:
                        code = c_ulong()
                        assert kernel.GetExitCodeProcess(handle, byref(code))
                        assert code.value != 259
                    finally:
                        kernel.CloseHandle(handle)
            assert list((tmp_path / 'parser-staging').iterdir()) == []
        finally:
            await parser.close()
    asyncio.run(exercise())
