import hashlib
import json

import pytest

from agent.ocr.store import OcrModelStore
from agent.storage import CoreDataError


def model_bundle(path):
    path.mkdir()
    weight = path / 'inference.pdiparams'
    weight.write_bytes(b'fixture weights')
    (path / 'ocr-model.json').write_text(json.dumps({
        'schema_version': 1, 'profile': 'pp-structure-v3', 'name': 'Fixture OCR',
        'license': 'Apache-2.0', 'models': {'text_detection': 'det'},
        'files': {'inference.pdiparams': hashlib.sha256(weight.read_bytes()).hexdigest()},
    }), encoding='utf-8')
    return path


def test_import_validates_and_copies_bundle(tmp_path):
    source = model_bundle(tmp_path / 'source')
    store = OcrModelStore(tmp_path / 'managed')
    inspection = store.inspect(source)
    assert inspection['valid']
    imported = store.import_bundle(source)
    assert imported['id'] == inspection['digest']
    source.joinpath('inference.pdiparams').unlink()
    assert store.get_profile(imported['id'])['name'] == 'Fixture OCR'
    assert len(store.list_models()['models']) == 1


def test_corrupt_bundle_is_rejected_without_registration(tmp_path):
    source = model_bundle(tmp_path / 'source')
    source.joinpath('inference.pdiparams').write_bytes(b'corrupt')
    store = OcrModelStore(tmp_path / 'managed')
    with pytest.raises(CoreDataError, match='hash'):
        store.import_bundle(source)
    assert store.list_models()['models'] == []


def test_model_manifest_cannot_escape_directory(tmp_path):
    source = model_bundle(tmp_path / 'source')
    manifest = json.loads(source.joinpath('ocr-model.json').read_text())
    manifest['files'] = {'../outside': 'a' * 64}
    source.joinpath('ocr-model.json').write_text(json.dumps(manifest))
    with pytest.raises(CoreDataError):
        OcrModelStore(tmp_path / 'managed').import_bundle(source)
