import hashlib
import json

import pytest


def setup_transfer(tmp_path):
    source = tmp_path / 'old-profile/models/identity'
    target = tmp_path / 'new-models/identity'
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    for root in (source, target):
        (root / 'model.bin').write_bytes(b'model weights')
    digest = hashlib.sha256(b'model weights').hexdigest()
    return {'source_root': str(source), 'target_root': str(target), 'profile_root': str(tmp_path / 'old-profile'),
            'files': {'model.bin': digest}, 'metadata_backups': {}}, source, target


def test_exclusive_weights_move_only_after_health_and_restore_is_verified(tmp_path):
    from agent.profile_model_transfer import finalize_model_transfer, restore_model_transfer
    record, source, target = setup_transfer(tmp_path)
    (source / 'keep-unknown.txt').write_bytes(b'unknown')
    with pytest.raises(ValueError, match='health'):
        finalize_model_transfer([record], tmp_path / 'job', health_verified=False, shared_roots=[])
    finalize_model_transfer([record], tmp_path / 'job', health_verified=True, shared_roots=[])
    assert not (source / 'model.bin').exists()
    assert (target / 'model.bin').read_bytes() == b'model weights'
    assert (source / 'keep-unknown.txt').read_bytes() == b'unknown'
    restore_model_transfer(tmp_path / 'job')
    assert (source / 'model.bin').read_bytes() == b'model weights'


def test_shared_external_and_changed_sources_are_preserved(tmp_path):
    from agent.profile_model_transfer import finalize_model_transfer
    record, source, target = setup_transfer(tmp_path)
    result = finalize_model_transfer([record], tmp_path / 'shared', health_verified=True, shared_roots=[source.parent])
    assert (source / 'model.bin').is_file()
    assert result['preserved'][0]['reason'] == 'shared'
    external = {**record, 'profile_root': str(tmp_path / 'another-profile')}
    result = finalize_model_transfer([external], tmp_path / 'external', health_verified=True, shared_roots=[])
    assert result['preserved'][0]['reason'] == 'external'
    (source / 'model.bin').write_bytes(b'changed')
    result = finalize_model_transfer([record], tmp_path / 'changed', health_verified=True, shared_roots=[])
    assert result['preserved'][0]['reason'] == 'changed'
    assert (source / 'model.bin').read_bytes() == b'changed'


def test_registered_other_profile_shared_storage_is_detected_read_only(tmp_path):
    from agent.profile_model_transfer import find_shared_model_roots
    record, source, target = setup_transfer(tmp_path)
    other = tmp_path / 'punkagent'
    (other / 'rag').mkdir(parents=True)
    registry = {'schema_version': 1, 'root': str(source.parent), 'models': []}
    path = other / 'rag/rag-models.json'
    path.write_text(json.dumps(registry), encoding='utf8')
    before = path.read_bytes()
    roots, warnings = find_shared_model_roots([other])
    assert source.parent.resolve() in roots
    assert not warnings
    assert path.read_bytes() == before


def test_interrupted_model_transfer_resumes_its_verified_journal(tmp_path):
    from agent.profile_model_transfer import finalize_model_transfer
    record, source, target = setup_transfer(tmp_path)
    for root in (source, target):
        (root / 'second.bin').write_bytes(b'second')
    record['files']['second.bin'] = hashlib.sha256(b'second').hexdigest()
    def cancelled():
        if not (source / 'model.bin').exists():
            raise InterruptedError('cancelled')
    with pytest.raises(InterruptedError):
        finalize_model_transfer([record], tmp_path / 'job', health_verified=True, shared_roots=[], check_cancel=cancelled)
    result = finalize_model_transfer([record], tmp_path / 'job', health_verified=True, shared_roots=[])
    assert len(result['files']) == 2
    assert not (source / 'second.bin').exists()
    assert finalize_model_transfer([record], tmp_path / 'job', health_verified=True, shared_roots=[]) == result
