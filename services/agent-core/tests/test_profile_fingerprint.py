import threading
import uuid

from agent.local_models.catalog import Catalog
from agent.storage import CoreRepository
from test_local_models_gguf import gguf_file


def test_migration_catalog_read_preserves_active_model_credentials_and_state(tmp_path):
    repo = CoreRepository(tmp_path / 'agent.db')
    catalog = Catalog(repo)
    model = catalog.import_model(gguf_file(tmp_path / 'model.gguf'), uuid.uuid4().hex, threading.Event())
    catalog.set_status(model['id'], 'loaded')
    credentials = catalog.root / '.credentials'
    credentials.mkdir()
    key = credentials / ('session-' + 'a' * 32 + '.key')
    key.write_bytes(b'owned active session')
    readonly = Catalog(repo, recover=False)
    readonly.verify(readonly.get(model['id']))
    assert readonly.get(model['id'])['status'] == 'loaded'
    assert key.read_bytes() == b'owned active session'


def test_source_fingerprint_ignores_model_residency_but_detects_configuration_changes(tmp_path):
    from agent.profile_fingerprint import profile_state
    repo = CoreRepository(tmp_path / 'agent.db')
    catalog = Catalog(repo)
    model = catalog.import_model(gguf_file(tmp_path / 'model.gguf'), uuid.uuid4().hex, threading.Event())
    catalog.set_status(model['id'], 'loaded')
    before = profile_state(tmp_path)
    catalog.set_status(model['id'], 'imported')
    assert profile_state(tmp_path) == before
    with repo._connect() as connection:
        connection.execute('UPDATE local_models SET context_length=context_length+1 WHERE id=?', (model['id'],))
    assert profile_state(tmp_path) != before
