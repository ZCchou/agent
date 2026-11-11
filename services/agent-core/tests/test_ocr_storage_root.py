import json

import pytest

from agent.ocr.store import OcrModelStore
from agent.storage import CoreDataError


def test_pipeline_and_worker_resolve_the_same_selected_ocr_model_root(tmp_path):
    data_dir = tmp_path / 'rag'
    data_dir.mkdir()
    root = tmp_path / 'selected-models' / 'ocr'
    (data_dir / 'ocr-storage.json').write_text(json.dumps({'schema_version': 1, 'root': str(root)}), encoding='utf8')
    assert OcrModelStore(data_dir / 'ocr-models').root == root.resolve()
    assert root.is_dir()


@pytest.mark.parametrize('value', [{'schema_version': 2, 'root': 'bad'}, {'schema_version': 1, 'root': '../other'}])
def test_invalid_ocr_storage_configuration_is_explicit(tmp_path, value):
    (tmp_path / 'ocr-storage.json').write_text(json.dumps(value), encoding='utf8')
    with pytest.raises(CoreDataError) as error:
        OcrModelStore(tmp_path / 'ocr-models')
    assert error.value.code == 'ocr_model_root_invalid'
