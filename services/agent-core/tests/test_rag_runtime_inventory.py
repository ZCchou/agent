import hashlib
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from agent.rag import models
from agent.storage import CoreDataError


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / 'app' / 'rag-runtime'
    payloads = {'rag-worker.exe': b'worker', '_internal/pkg/中文.dll': b'library',
                '_internal/pkg/data.txt': b'public data'}
    for name, content in payloads.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (root / 'empty').mkdir()
    (root / 'runtime.json').write_text(json.dumps({'schema_version': 1,
        'protocol_version': 2, 'executable': 'rag-worker.exe',
        'files': {name: hashlib.sha256(content).hexdigest() for name, content in payloads.items()}}))
    return root, payloads


def scan(root):
    counts = {'entries': 0, 'files': 0}
    def tick(key):
        counts[key] += 1
    actual, stamp = models._runtime_inventory(root, root / 'runtime.json', SimpleNamespace(
        entry_checked=lambda: tick('entries'), file_processed=lambda: tick('files')))
    return actual, dict((row[0], row[1:]) for row in stamp), counts


def test_cold_hash_checks_every_file_and_resolves_the_trusted_root_only_once(runtime, tmp_path, monkeypatch):
    root, payloads=runtime
    original = Path.resolve
    root_resolves=[]
    def observe(path, *args, **kwargs):
        if path == root:
            root_resolves.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', observe)
    hashes={name:hashlib.sha256(data).hexdigest() for name,data in payloads.items()}
    for i in range(70):
        name=f'_internal/data/{i}.txt'
        path=root/name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'check every file')
        hashes[name]=hashlib.sha256(b'check every file').hexdigest()
    _,stamp,_=scan(root)
    progress=models._IntegrityProgress('runtime_hashes',len(hashes))
    sizes={name:identity[1] for name,identity in stamp.items()}
    models._verify_runtime_hashes(root,hashes,sizes,progress)
    assert root_resolves == [root]
    assert progress.files_processed == len(hashes)
    assert progress.bytes_read == sum((root/name).stat().st_size for name in hashes)
    (root/name).write_bytes(b'changed')
    with pytest.raises(ValueError,match='hash mismatch'):
        models._verify_runtime_hashes(root,hashes,sizes,progress)


def test_inventory_preserves_full_names_file_ids_times_and_progress(runtime):
    root, payloads = runtime
    actual, stamp, counts = scan(root)
    assert actual == set(payloads)
    assert set(stamp) == set(payloads) | {'runtime.json'}
    for name in stamp:
        st = (root / name).stat()
        assert stamp[name] == (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    assert counts == {'entries': 7, 'files': 3}


def proxy_scan(monkeypatch, transform):
    original = models.os.scandir
    class Entry:
        def __init__(self, entry):
            self.entry = entry
            self.name, self.path = entry.name, entry.path
        def stat(self, *, follow_symlinks):
            assert follow_symlinks is False
            return transform(self.entry, self.entry.stat(follow_symlinks=False))
    class Scan:
        def __init__(self, directory):
            self.inner = original(directory)
        def __enter__(self):
            return (Entry(entry) for entry in self.inner)
        def __exit__(self, *args):
            self.inner.close()
    monkeypatch.setattr(models.os, 'scandir', Scan)


def test_zero_inode_direntry_still_uses_real_file_identity(runtime, monkeypatch):
    root, payloads = runtime
    def zero_inode(entry, st):
        return SimpleNamespace(st_mode=st.st_mode, st_file_attributes=getattr(st, 'st_file_attributes', 0),
                               st_ino=0, st_size=st.st_size, st_mtime_ns=st.st_mtime_ns, st_ctime_ns=st.st_ctime_ns)
    proxy_scan(monkeypatch, zero_inode)
    _, records, _ = scan(root)
    for name in payloads:
        assert records[name][0] == (root / name).stat().st_ino
        assert records[name][0] != 0


@pytest.mark.parametrize('target', ['empty', '_internal/pkg/中文.dll'])
def test_all_reparse_attributes_are_rejected_before_descent_or_file_use(runtime, monkeypatch, target):
    root, _ = runtime
    def reparse(entry, st):
        if Path(entry.path) == root / target:
            return SimpleNamespace(st_mode=st.st_mode, st_file_attributes=0x400)
        return st
    proxy_scan(monkeypatch, reparse)
    with pytest.raises(ValueError, match='Linked'):
        scan(root)


def test_linked_runtime_root_is_rejected(runtime, monkeypatch):
    root, _ = runtime
    original = models._linked
    monkeypatch.setattr(models, '_linked', lambda path: True if path == root else original(path))
    with pytest.raises(ValueError, match='Linked'):
        scan(root)


def test_symlink_mode_is_rejected_even_without_windows_reparse_attribute(runtime, monkeypatch):
    def linked(entry, st):
        return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
    proxy_scan(monkeypatch, linked)
    with pytest.raises(ValueError, match='Linked'):
        scan(runtime[0])


def test_real_directory_link_is_not_followed(runtime, tmp_path):
    root, _ = runtime
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'payload.dll').write_bytes(b'outside runtime')
    try:
        os.symlink(outside, root / 'linked-dir', target_is_directory=True)
    except OSError as error:
        if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
            pytest.skip('Windows directory symlink requires privilege (winerror=1314): ' + str(error))
        raise
    with pytest.raises(ValueError, match='Linked'):
        scan(root)


@pytest.mark.parametrize('kind', ['open', 'iterate', 'stat'])
def test_enumeration_and_stat_errors_fail_closed(runtime, monkeypatch, kind):
    root, _ = runtime
    original = models.os.scandir
    class BrokenScan:
        def __init__(self, directory):
            if kind == 'open':
                raise PermissionError('inaccessible directory')
            self.inner = original(directory)
        def __enter__(self):
            if kind == 'iterate':
                def rows():
                    yield next(self.inner)
                    raise OSError('directory disappeared')
                return rows()
            return self.inner
        def __exit__(self, *args):
            self.inner.close()
    if kind == 'stat':
        def broken(entry, st):
            raise PermissionError('entry inaccessible')
        proxy_scan(monkeypatch, broken)
    else:
        monkeypatch.setattr(models.os, 'scandir', BrokenScan)
    with pytest.raises(OSError):
        scan(root)


def configured_store(runtime, tmp_path, monkeypatch):
    root, _ = runtime
    monkeypatch.setattr(models, 'sys', SimpleNamespace(frozen=True,
        executable=str(root.parent / 'agent-core.exe')))
    return models.ModelStore(tmp_path / 'store')


@pytest.mark.parametrize('change', ['extra', 'missing', 'content'])
def test_cached_runtime_rejects_added_removed_or_changed_file(runtime, tmp_path, monkeypatch, change):
    root, _ = runtime
    store = configured_store(runtime, tmp_path, monkeypatch)
    store._command()
    if change == 'extra':
        (root / '_internal/new.dll').write_bytes(b'not listed')
    elif change == 'missing':
        (root / '_internal/pkg/data.txt').unlink()
    else:
        (root / '_internal/pkg/data.txt').write_bytes(b'tampered content')
    with pytest.raises(CoreDataError, match='unlisted or missing|hash mismatch'):
        store._command()


def test_cache_hit_skips_hash_but_force_rehashes_all_files(runtime, tmp_path, monkeypatch):
    _, payloads = runtime
    store = configured_store(runtime, tmp_path, monkeypatch)
    seen = []
    original = models._hash
    def counted(path, **kwargs):
        seen.append(path)
        return original(path, **kwargs)
    monkeypatch.setattr(models, '_hash', counted)
    store._command()
    assert len(seen) == len(payloads)
    seen.clear()
    store._command()
    assert seen == []
    store._command(force=True)
    assert len(seen) == len(payloads)


def test_new_file_during_cold_hash_cannot_enter_verified_cache(runtime,tmp_path,monkeypatch):
    root,_=runtime
    store=configured_store(runtime,tmp_path,monkeypatch)
    original=models._verify_runtime_hashes
    def add_after_hash(*args):
        original(*args)
        (root/'new-library.dll').write_bytes(b'unlisted')
    monkeypatch.setattr(models,'_verify_runtime_hashes',add_after_hash)
    with pytest.raises(CoreDataError,match='changed during hash verification'):
        store._command()
    assert store._runtime_verified is None


def test_cache_hit_does_not_repeat_path_link_queries_for_every_inventory_entry(runtime, tmp_path, monkeypatch):
    store = configured_store(runtime, tmp_path, monkeypatch)
    store._command()
    calls = []
    original = models._linked
    def counted(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(models, '_linked', counted)
    store._command()
    # The command may check the root and executable. All inventory entries
    # still require their metadata, but not repeated Path link syscalls.
    assert len(calls) <= 2


def test_force_rejects_changed_content_even_after_restoring_size_and_mtime(runtime, tmp_path, monkeypatch):
    root, _ = runtime
    store = configured_store(runtime, tmp_path, monkeypatch)
    store._command()
    target = root / '_internal/pkg/data.txt'
    before = target.stat()
    target.write_bytes(b'x' * before.st_size)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(CoreDataError, match='hash mismatch'):
        store._command(force=True)


def test_atomic_inode_replacement_invalidates_cached_stamp(runtime, tmp_path, monkeypatch):
    root, payloads = runtime
    store = configured_store(runtime, tmp_path, monkeypatch)
    store._command()
    before = store._runtime_verified
    target = root / '_internal/pkg/data.txt'
    original_stat = target.stat()
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(target.read_bytes())
    os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    os.replace(replacement, target)
    assert target.stat().st_ino != original_stat.st_ino
    seen = []
    original = models._hash
    def counted(path, **kwargs):
        seen.append(path)
        return original(path, **kwargs)
    monkeypatch.setattr(models, '_hash', counted)
    store._command()
    assert store._runtime_verified != before
    assert len(seen) == len(payloads)


def test_scan_permission_failure_is_not_treated_as_cached_success(runtime, tmp_path, monkeypatch):
    store = configured_store(runtime, tmp_path, monkeypatch)
    store._command()
    before = store._runtime_verified
    def denied(directory):
        raise PermissionError('scan denied')
    monkeypatch.setattr(models.os, 'scandir', denied)
    with pytest.raises(CoreDataError, match='scan denied'):
        store._command()
    assert store._runtime_verified == before
