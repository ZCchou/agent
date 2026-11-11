import hashlib

import pytest

from agent.storage import CoreDataError


def test_copy_checks_identity_and_reuses_only_an_identical_destination(tmp_path):
    from agent.profile_files import copy_managed_file
    source, destination = tmp_path / '源 文件.docx', tmp_path / 'target' / '资料.docx'
    source.write_bytes(b'original bytes')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert copy_managed_file(source, destination, digest, source.stat().st_size)
    assert not copy_managed_file(source, destination, digest, source.stat().st_size)
    destination.write_bytes(b'unexpected')
    with pytest.raises(CoreDataError) as error:
        copy_managed_file(source, destination, digest, source.stat().st_size)
    assert error.value.code == 'migration_file_conflict'
    assert destination.read_bytes() == b'unexpected'


def test_cancel_and_hash_failure_leave_no_partial_published_file(tmp_path):
    from agent.profile_files import copy_managed_file
    source, destination = tmp_path / 'source.bin', tmp_path / 'target.bin'
    source.write_bytes(b'x' * (3 * 1024 * 1024))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise InterruptedError('cancelled')
    with pytest.raises(InterruptedError):
        copy_managed_file(source, destination, digest, source.stat().st_size, check_cancel=cancelled)
    assert not destination.exists()
    assert not list(tmp_path.glob('*.tmp'))
    with pytest.raises(CoreDataError):
        copy_managed_file(source, destination, '0'*64, source.stat().st_size)
    assert not destination.exists()
    assert source.is_file()


def test_copy_reports_monotonic_actual_file_bytes(tmp_path):
    from agent.profile_files import copy_managed_file
    source, destination = tmp_path / 'weights.bin', tmp_path / 'target.bin'
    source.write_bytes(b'x' * (2 * 1024 * 1024 + 7))
    events = []
    size = source.stat().st_size
    copy_managed_file(source, destination, hashlib.sha256(source.read_bytes()).hexdigest(), size,
                      progress=lambda completed, total: events.append((completed, total)))
    assert events[-1] == (size, size)
    assert all(0 <= completed <= total == size for completed, total in events)
    assert [value[0] for value in events] == sorted(value[0] for value in events)
