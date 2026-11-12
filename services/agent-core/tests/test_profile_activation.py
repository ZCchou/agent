from pathlib import Path
import json
import gc
import sqlite3
from contextlib import closing

import pytest

from agent.storage import CoreDataError, CoreRepository


def candidate(root):
    root.mkdir()
    CoreRepository(root / 'agent.db').create_project('Migrated', 'work')
    (root / 'settings.json').write_text('{"settings":{}}', encoding='utf8')
    (root / 'message-attachments').mkdir()
    (root / 'message-attachments/source.docx').write_bytes(b'original')
    (root / 'original.db').write_bytes(b'private backup')
    gc.collect()  # Represents the stopped Core releasing its SQLite handles.
    return root


def test_activation_health_failure_restores_old_database_and_settings(tmp_path):
    from agent.profile_activation import activate_candidate
    target = tmp_path / 'target'
    target.mkdir()
    CoreRepository(target / 'agent.db').create_project('Original', 'work')
    (target / 'settings.json').write_bytes(b'old settings')
    gc.collect()
    before = (target / 'agent.db').read_bytes()
    selected = candidate(tmp_path / 'candidate')
    def health():
        with closing(sqlite3.connect(target / 'agent.db')) as connection:
            assert connection.execute('SELECT name FROM projects').fetchone()[0] == 'Migrated'
        raise RuntimeError('handshake failed')
    with pytest.raises(RuntimeError, match='handshake failed'):
        activate_candidate(selected, target, tmp_path / 'job', health_check=health)
    assert (target / 'agent.db').read_bytes() == before
    assert (target / 'settings.json').read_bytes() == b'old settings'
    assert not (target / 'message-attachments/source.docx').exists()
    assert not (target / 'original.db').exists()
    assert json.loads((tmp_path / 'job/activation.json').read_text())['status'] == 'rolled_back'


def test_success_preserves_shared_model_root_and_requires_explicit_rollback(tmp_path):
    from agent.profile_activation import activate_candidate, rollback_activation
    target = tmp_path / 'target'
    target.mkdir()
    CoreRepository(target / 'agent.db')
    (target / 'models').mkdir()
    (target / 'models/shared.bin').write_bytes(b'shared with another application')
    selected = candidate(tmp_path / 'candidate')
    activate_candidate(selected, target, tmp_path / 'job', health_check=lambda: None)
    assert CoreRepository(target / 'agent.db').list_projects()[0]['name'] == 'Migrated'
    assert (target / 'models/shared.bin').read_bytes() == b'shared with another application'
    gc.collect()
    rollback_activation(tmp_path / 'job')
    assert CoreRepository(target / 'agent.db').list_projects() == []
    assert not (target / 'settings.json').exists()
    assert (target / 'models/shared.bin').is_file()


def test_rollback_accepts_configuration_serialization_changes_but_rejects_user_edits(tmp_path):
    from agent.profile_activation import activate_candidate, rollback_activation
    selected = candidate(tmp_path / 'candidate')
    target = tmp_path / 'target'
    target.mkdir()
    (target / 'settings.json').write_bytes(b'{"settings":{"old":true}}')
    job = tmp_path / 'job'
    activate_candidate(selected, target, job, health_check=lambda: None)
    (target / 'settings.json').write_text('{\n  "settings": {}\n}\n', encoding='utf8')
    rollback_activation(job)
    assert (target / 'settings.json').read_bytes() == b'{"settings":{"old":true}}'
    second = tmp_path / 'second-job'
    activate_candidate(selected, target, second, health_check=lambda: None)
    (target / 'settings.json').write_text('{"settings":{"user_edit":true}}', encoding='utf8')
    with pytest.raises(CoreDataError, match='已有更改'):
        rollback_activation(second)
    assert json.loads((target / 'settings.json').read_text())['settings']['user_edit'] is True


def test_activation_refuses_blob_conflicts_and_cancel_does_not_change_target(tmp_path):
    from agent.profile_activation import activate_candidate
    selected = candidate(tmp_path / 'candidate')
    target = tmp_path / 'target'
    (target / 'message-attachments').mkdir(parents=True)
    (target / 'message-attachments/source.docx').write_bytes(b'different original')
    with pytest.raises(CoreDataError, match='已有不同内容'):
        activate_candidate(selected, target, tmp_path / 'job', health_check=lambda: None)
    assert not (target / 'agent.db').exists()
    def cancelled():
        raise InterruptedError('cancelled')
    with pytest.raises(InterruptedError):
        activate_candidate(selected, target, tmp_path / 'cancel-job', health_check=lambda: None, check_cancel=cancelled)


def test_activation_real_core_health_subprocess_leaves_a_recoverable_database(tmp_path):
    import subprocess
    import sys
    import os
    from agent.profile_activation import activate_candidate, rollback_activation
    selected = candidate(tmp_path / 'candidate')
    target = tmp_path / 'target'
    target.mkdir()
    CoreRepository(target / 'agent.db')
    gc.collect()
    def health():
        script = 'from agent.storage import CoreRepository; from pathlib import Path; import sys; CoreRepository(Path(sys.argv[1]))'
        environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
        subprocess.run([sys.executable, '-c', script, str(target / 'agent.db')], check=True, timeout=30, env=environment)
    activate_candidate(selected, target, tmp_path / 'job', health_check=health)
    rollback_activation(tmp_path / 'job')
    assert not CoreRepository(target / 'agent.db').list_projects()
