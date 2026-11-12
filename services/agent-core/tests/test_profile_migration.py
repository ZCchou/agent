import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import uuid
import asyncio
import threading

from agent.storage import CoreRepository


def source_profile(root):
    root.mkdir()
    repo = CoreRepository(root / 'agent.db')
    project = repo.create_project('Project', 'work')
    chat = repo.create_conversation('project_work', 'Chat', project_id=project['id'])
    identity = str(uuid.uuid4())
    content = b'word attachment payload'
    key = f'message-attachments/{identity}.docx'
    path = root / key
    path.parent.mkdir()
    path.write_bytes(content)
    attachment = {'id': identity, 'kind': 'text', 'name': '试验模板.docx',
        'media_type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'byte_size': len(content), 'sha256': hashlib.sha256(content).hexdigest(), 'storage_key': key,
        'width': None, 'height': None, 'purpose': 'template'}
    repo.start_model_run(content='Fill template', idempotency_key='source-request', provider_profile_id='p', model_id='m',
                         conversation_id=chat['id'], attachments=[attachment])
    (root / 'message-attachments' / (identity + '.json')).write_text(json.dumps(attachment), encoding='utf8')
    return identity


def test_candidate_preserves_sources_and_copies_conflicting_attachment_manifests(tmp_path):
    from agent.profile_migration import MigrationCoordinator
    primary, secondary = tmp_path / 'document', tmp_path / 'ocr'
    attachment_id = source_profile(primary)
    shutil.copytree(primary, secondary)
    with sqlite3.connect(secondary / 'agent.db') as connection:
        connection.execute("UPDATE messages SET content='Different source task' WHERE role='user'")
    target = tmp_path / 'unified'
    target.mkdir()
    CoreRepository(target / 'agent.db')
    before = (primary / f'message-attachments/{attachment_id}.docx').read_bytes()
    migration = MigrationCoordinator(target, tmp_path / 'models', tmp_path / 'migration-job')
    result = migration.prepare([{'root': str(primary), 'label': 'Document Preview'}, {'root': str(secondary), 'label': 'OCR Preview'}])
    candidate = Path(result['candidate_root'])
    with sqlite3.connect(candidate / 'agent.db') as connection:
        rows = connection.execute('SELECT id,storage_key FROM message_attachments').fetchall()
        assert len(rows) == 2
        assert len({row[0] for row in rows}) == 2
        for identity, key in rows:
            assert (candidate / key).read_bytes() == before
            manifest = json.loads((candidate / 'message-attachments' / (identity + '.json')).read_text(encoding='utf8'))
            assert manifest['id'] == identity
            assert manifest['name'] == '试验模板.docx'
            assert manifest['storage_key'] == key
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
    with sqlite3.connect(target / 'agent.db') as connection:
        assert connection.execute('SELECT count(*) FROM projects').fetchone()[0] == 0
    assert (primary / f'message-attachments/{attachment_id}.docx').read_bytes() == before
    state = json.loads((tmp_path / 'migration-job' / 'state.json').read_text(encoding='utf8'))
    assert state['phase'] == 'prepared'
    assert 'completed_bytes' not in state


def test_auxiliary_models_are_verified_copied_and_referenced_from_selected_storage(tmp_path):
    from agent.profile_migration import MigrationCoordinator
    from agent.rag.models import ModelStore
    from test_rag_models import model_directory
    source = tmp_path / 'source'
    source_profile(source)
    CoreRepository(source / 'agent.db').update_global_instructions('Use Document Preview preferences.', enabled=True)
    store = ModelStore(source / 'rag')
    model = asyncio.run(store.import_model(model_directory(tmp_path / 'weights'), 'bge-m3'))
    target = tmp_path / 'unified'
    target.mkdir()
    models = tmp_path / 'selected-models'
    result = MigrationCoordinator(target, models, tmp_path / 'migration-job').prepare([{'root': str(source), 'label': 'Document Preview'}])
    candidate = Path(result['candidate_root'])
    registry = json.loads((candidate / 'rag/rag-models.json').read_text(encoding='utf8'))
    assert Path(registry['root']) == models / 'auxiliary'
    assert registry['models'][0]['digest'] == model['digest']
    ModelStore(candidate / 'rag')._verify(registry['models'][0])
    assert (source / 'rag/models' / model['id']).is_dir()
    assert result['model_sources']


def test_main_and_ocr_models_keep_identity_and_use_selected_storage(tmp_path):
    from agent.profile_migration import MigrationCoordinator
    from agent.local_models.catalog import Catalog
    from agent.ocr.store import OcrModelStore
    from test_local_models_gguf import gguf_file
    from test_ocr_models import model_bundle
    source = tmp_path / 'source'
    source_profile(source)
    catalog = Catalog(CoreRepository(source / 'agent.db'))
    main = catalog.import_model(gguf_file(tmp_path / '千问.gguf'), uuid.uuid4().hex, threading.Event())
    ocr = OcrModelStore(source / 'rag/ocr-models').import_bundle(model_bundle(tmp_path / 'ocr-package'))
    models = tmp_path / 'models'
    result = MigrationCoordinator(tmp_path / 'target', models, tmp_path / 'migration-job').prepare([{'root': str(source), 'label': 'Document Preview'}])
    candidate = Path(result['candidate_root'])
    merged = Catalog(CoreRepository(candidate / 'agent.db'))
    assert merged.root == models / 'main'
    merged.verify(merged.get(main['id']))
    assert OcrModelStore(candidate / 'rag/ocr-models').get_profile(ocr['id'])['digest'] == ocr['digest']
    assert (source / 'local-models/objects' / main['id']).is_dir()
    assert (source / 'rag/ocr-models' / ocr['id']).is_dir()
    assert {record['kind'] for record in result['model_sources']} == {'main', 'ocr'}
    from agent.profile_model_transfer import finalize_model_transfer
    finalize_model_transfer(result['model_sources'], tmp_path / 'transfer', health_verified=True, shared_roots=[])
    repeated = MigrationCoordinator(tmp_path / 'target', models, tmp_path / 'following-job').prepare([{'root': str(source), 'label': 'Document Preview'}])
    assert OcrModelStore(Path(repeated['candidate_root']) / 'rag/ocr-models').get_profile(ocr['id'])['digest'] == ocr['digest']


def test_stale_secondary_model_path_reuses_only_the_verified_matching_identity(tmp_path):
    from agent.profile_migration import MigrationCoordinator
    from agent.rag.models import ModelStore
    from test_rag_models import model_directory
    primary, secondary = tmp_path / 'primary', tmp_path / 'secondary'
    source_profile(primary)
    store = ModelStore(primary / 'rag')
    record = asyncio.run(store.import_model(model_directory(tmp_path / 'weights'), 'bge-m3'))
    shutil.copytree(primary, secondary)
    registry = json.loads((secondary / 'rag/rag-models.json').read_text(encoding='utf8'))
    registry['root'] = str(tmp_path / 'removed-qa-models')
    (secondary / 'rag/rag-models.json').write_text(json.dumps(registry), encoding='utf8')
    migration = MigrationCoordinator(tmp_path / 'target', tmp_path / 'models', tmp_path / 'job')
    result = migration.prepare([{'root': str(primary), 'label': 'Document Preview'}, {'root': str(secondary), 'label': 'OCR Preview'}])
    loaded = ModelStore(Path(result['candidate_root']) / 'rag')
    assert len(loaded.list_models()) == 1
    loaded._verify(loaded.list_models()[0])
    assert loaded.list_models()[0]['digest'] == record['digest']
    assert any(warning['code'] == 'source_model_repaired' for warning in migration.state['warnings'])


def test_prepare_keeps_raw_snapshot_and_merges_configuration_with_final_paths(tmp_path):
    from agent.profile_migration import MigrationCoordinator
    source = tmp_path / 'source'
    source_profile(source)
    CoreRepository(source / 'agent.db').update_global_instructions('Use Document Preview preferences.', enabled=True)
    settings = {'settings': {'general': {'interface_language': 'zh-CN'}, 'models': {'providers': []}}}
    (source / 'settings.json').write_text(json.dumps(settings), encoding='utf8')
    with sqlite3.connect(source / 'agent.db') as connection:
        project_id = connection.execute('SELECT id FROM projects').fetchone()[0]
    grants = {'projects': {project_id: {'directory': str(tmp_path / 'project'), 'active': True,
              'root_generation': 1, 'suspended_at': None}}, 'works': {}}
    (source / 'work-tool-grants.json').write_text(json.dumps(grants), encoding='utf8')
    target = tmp_path / 'target'
    result = MigrationCoordinator(target, tmp_path / 'models', tmp_path / 'job').prepare([{'root': str(source), 'label': 'Document'}])
    candidate = Path(result['candidate_root'])
    assert json.loads((candidate / 'settings.json').read_text(encoding='utf8')) == settings
    assert json.loads((candidate / 'work-tool-grants.json').read_text(encoding='utf8')) == grants
    raw = Path(result['profiles'][0]['raw_snapshot'])
    assert raw.is_file()
    with sqlite3.connect(raw) as connection:
        assert connection.execute('SELECT id FROM projects').fetchone()[0] == project_id
        assert connection.execute('SELECT status FROM model_runs').fetchone()[0] == 'queued'
    with sqlite3.connect(candidate / 'agent.db') as connection:
        assert connection.execute('SELECT status FROM model_runs').fetchone()[0] == 'interrupted'
    assert not (target / 'settings.json').exists()
    assert CoreRepository(candidate / 'agent.db').get_global_instructions()['content'] == 'Use Document Preview preferences.'
