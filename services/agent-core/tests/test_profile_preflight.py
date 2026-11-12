from collections import namedtuple
import sqlite3

import pytest

from agent.storage import CoreDataError, CoreRepository


def test_insufficient_profile_volume_stops_before_any_candidate_or_model_copy(tmp_path, monkeypatch):
    from agent.profile_migration import MigrationCoordinator
    monkeypatch.chdir(tmp_path)
    source = tmp_path / 'source'
    CoreRepository(source / 'agent.db').create_project('Old', 'work')
    target, models, workspace = tmp_path / 'target', tmp_path / 'models', tmp_path / 'workspace'
    usage = namedtuple('usage', 'total used free')
    monkeypatch.setattr('shutil.disk_usage', lambda path: usage(10**12, 1, 1 if 'models' not in str(path) else 10**11))
    coordinator = MigrationCoordinator(target, models, workspace)
    with pytest.raises(CoreDataError) as error:
        coordinator.prepare([{'root': str(source), 'label': 'Old'}])
    assert error.value.code == 'migration_insufficient_space'
    assert not list(workspace.glob('attempt-*'))
    assert not models.exists()


def test_readonly_preview_reports_conflicts_and_both_storage_requirements(tmp_path, monkeypatch):
    from agent.profile_preflight import preview
    monkeypatch.chdir(tmp_path)
    source, other = tmp_path / 'source', tmp_path / 'other'
    repo = CoreRepository(source / 'agent.db')
    project = repo.create_project('First', 'work')
    other.mkdir()
    with sqlite3.connect(source / 'agent.db') as old, sqlite3.connect(other / 'agent.db') as new:
        old.backup(new)
        new.execute('UPDATE projects SET name=? WHERE id=?', ('Conflicting', project['id']))
    result = preview(tmp_path / 'target', tmp_path / 'models', tmp_path / 'workspace',
                     [{'root': str(source), 'label': 'First'}, {'root': str(other), 'label': 'Second'}])
    assert result['conflicting_groups'] == 1
    assert {item['purpose'] for item in result['storage']} == {'profile', 'models'}
    assert all(item['required_bytes'] > 0 and item['available_bytes'] > 0 for item in result['storage'])
    assert not (tmp_path / 'workspace').exists()
    assert not (tmp_path / 'models').exists()
