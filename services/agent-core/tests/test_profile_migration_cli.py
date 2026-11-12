import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

from agent.storage import CoreRepository


def invoke(*arguments):
    return subprocess.run([sys.executable, '-m', 'agent.main', *map(str, arguments)],
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'), 'PYTHONUTF8': '1'},
        cwd=Path(arguments[-1]).parent, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding='utf8', timeout=45)


def test_health_command_checks_exact_core_contract_without_starting_models(tmp_path):
    from agent.protocol import BUILD_ID
    CoreRepository(tmp_path / 'agent.db')
    result = invoke('--profile-health', tmp_path)
    assert result.returncode == 0, result.stderr
    health = json.loads(result.stdout)
    assert health['schema_version'] == 30
    assert health['core_build_id'] == BUILD_ID
    assert health['integrity'] == 'ok'


@pytest.mark.parametrize('native_prefix', ['', '\\\\?\\'] if os.name == 'nt' else [''])
def test_preview_worker_reports_capacity_without_copying_models_or_creating_candidate(tmp_path, native_prefix):
    source = tmp_path / 'io.github.zcchou.agent.documentpreview'
    CoreRepository(source / 'agent.db')
    target = tmp_path / 'io.github.zcchou.agent.airworthiness'
    job = target / 'profile-migrations' / 'job'
    job.mkdir(parents=True)
    request = {'application_id': target.name, 'action': 'preview', 'target_root': native_prefix + str(target),
               'model_root': native_prefix + str(tmp_path / 'models'), 'workspace': native_prefix + str(job),
               'sources': [{'root': native_prefix + str(source), 'label': 'Document Preview'}]}
    path = job / 'request.json'
    path.write_text(json.dumps(request), encoding='utf8')
    result = invoke('--profile-migration', native_prefix + str(path))
    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads((job / 'state.json').read_text())
    assert state['status'] == 'preview_ready'
    assert len(state['preview']['storage']) == 2
    assert json.loads(path.read_text())['sources'][0]['root'] == state['preview']['sources'][0]['root']
    assert not (job / 'candidate.json').exists()
    assert not (tmp_path / 'models').exists()


def test_activation_requires_legacy_profile_to_be_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import pytest
    from agent.profile_migration_cli import run_request
    from agent.storage import CoreDataError
    source = tmp_path / 'io.github.zcchou.agent.documentpreview'
    CoreRepository(source / 'agent.db')
    target = tmp_path / 'io.github.zcchou.agent.airworthiness'
    job = target / 'profile-migrations' / 'job'
    job.mkdir(parents=True)
    request = {'application_id': target.name, 'action': 'prepare', 'target_root': str(target),
               'model_root': str(tmp_path / 'models'), 'workspace': str(job),
               'sources': [{'root': str(source), 'label': 'Document Preview'}]}
    path = job / 'request.json'
    path.write_text(json.dumps(request), encoding='utf8')
    run_request(path)
    request['action'] = 'activate'
    path.write_text(json.dumps(request), encoding='utf8')
    monkeypatch.setattr('agent.profile_model_transfer.legacy_profile_busy', lambda root: True)
    with pytest.raises(CoreDataError) as error:
        run_request(path)
    assert error.value.code == 'migration_application_busy'
    assert not (target / 'agent.db').exists()


def test_prepare_worker_persists_progress_and_honors_cancel_marker(tmp_path):
    source = tmp_path / 'io.github.zcchou.agent.documentpreview'
    CoreRepository(source / 'agent.db').create_project('Old project', 'work')
    target = tmp_path / 'io.github.zcchou.agent.airworthiness'
    job = target / 'profile-migrations' / 'job'
    job.mkdir(parents=True)
    request = {'application_id': 'io.github.zcchou.agent.airworthiness', 'action': 'prepare',
        'target_root': str(target), 'model_root': str(tmp_path / 'models'), 'workspace': str(job),
        'sources': [{'root': str(source), 'label': 'Document Preview'}]}
    path = job / 'request.json'
    path.write_text(json.dumps(request), encoding='utf8')
    result = invoke('--profile-migration', path)
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[-1]['status'] == 'prepared'
    assert (job / 'candidate.json').is_file()
    assert not (target / 'agent.db').exists()
    import gc
    gc.collect()  # Model the source Core having exited and released SQLite handles.
    request['action'] = 'activate'
    path.write_text(json.dumps(request), encoding='utf8')
    activated = invoke('--profile-migration', path)
    assert activated.returncode == 0, activated.stdout + activated.stderr
    assert json.loads(activated.stdout.splitlines()[-1])['status'] == 'awaiting_restart'
    assert CoreRepository(target / 'agent.db').list_projects()[0]['name'] == 'Old project'
    request['action'] = 'prepare'
    path.write_text(json.dumps(request), encoding='utf8')
    (job / 'cancel.flag').write_text('', encoding='utf8')
    result = invoke('--profile-migration', path)
    assert result.returncode == 2
    assert json.loads(result.stdout.splitlines()[-1])['status'] == 'cancelled'


def test_activation_refuses_source_changes_after_snapshot(tmp_path):
    source = tmp_path / 'io.github.zcchou.agent.documentpreview'
    repo = CoreRepository(source / 'agent.db')
    project = repo.create_project('Original name', 'work')
    target = tmp_path / 'io.github.zcchou.agent.airworthiness'
    job = target / 'profile-migrations' / 'job'
    job.mkdir(parents=True)
    request = {'application_id': 'io.github.zcchou.agent.airworthiness', 'action': 'prepare',
        'target_root': str(target), 'model_root': str(tmp_path / 'models'), 'workspace': str(job),
        'sources': [{'root': str(source), 'label': 'Document Preview'}]}
    path = job / 'request.json'
    path.write_text(json.dumps(request), encoding='utf8')
    assert invoke('--profile-migration', path).returncode == 0
    with repo._connect() as connection:
        connection.execute('UPDATE projects SET name=? WHERE id=?', ('New unsnapshotted name', project['id']))
    request['action'] = 'activate'
    path.write_text(json.dumps(request), encoding='utf8')
    result = invoke('--profile-migration', path)
    assert result.returncode == 1
    assert json.loads(result.stdout.splitlines()[-1])['error']['code'] == 'migration_source_changed'
    assert not (target / 'agent.db').exists()


def test_finalize_moves_only_verified_private_weights_after_activation(tmp_path):
    import gc
    import threading
    import uuid
    from agent.local_models.catalog import Catalog
    from test_local_models_gguf import gguf_file
    source = tmp_path / 'io.github.zcchou.agent.documentpreview'
    repo = CoreRepository(source / 'agent.db')
    catalog = Catalog(repo)
    model = catalog.import_model(gguf_file(tmp_path / 'model.gguf'), uuid.uuid4().hex, threading.Event())
    original = catalog.root / 'objects' / model['id'] / model['files'][0]['name']
    target = tmp_path / 'io.github.zcchou.agent.airworthiness'
    job = target / 'profile-migrations' / 'job'
    job.mkdir(parents=True)
    request = {'application_id': 'io.github.zcchou.agent.airworthiness', 'action': 'prepare',
        'target_root': str(target), 'model_root': str(tmp_path / 'models'), 'workspace': str(job),
        'sources': [{'root': str(source), 'label': 'Document Preview'}]}
    path = job / 'request.json'
    gc.collect()
    for action in ('prepare', 'activate', 'finalize'):
        request['action'] = action
        path.write_text(json.dumps(request), encoding='utf8')
        result = invoke('--profile-migration', path)
        assert result.returncode == 0, result.stdout + result.stderr
    assert not original.exists()
    assert Catalog(CoreRepository(target / 'agent.db'), recover=False).get(model['id'])['digest'] == model['digest']
    assert (job / 'model-transfer.json').is_file()
    following = target / 'profile-migrations' / 'following'
    following.mkdir()
    request.update(action='prepare', workspace=str(following))
    path = following / 'request.json'
    path.write_text(json.dumps(request), encoding='utf8')
    repeated = invoke('--profile-migration', path)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
