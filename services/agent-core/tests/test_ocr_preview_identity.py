import json
from pathlib import Path
import pytest

from agent.protocol import BUILD_ID, CORE_VERSION, PROTOCOL_VERSION
from agent.storage import SCHEMA_VERSION


def test_ocr_preview_has_separate_app_and_protocol_identity():
    root = Path(__file__).resolve().parents[3]
    config = json.loads((root / 'apps/agent-desktop/src-tauri/tauri.conf.json').read_text())
    assert config['identifier'] == 'io.github.zcchou.agent.ocrpreview'
    assert config['productName'] == 'agent OCR Preview'
    assert (CORE_VERSION, PROTOCOL_VERSION, SCHEMA_VERSION, BUILD_ID) == (
        '0.15.0', 22, 29, 'schema29-rag-workflow-v1')


@pytest.mark.parametrize('config_name', ['tauri.document-preview.conf.json', 'tauri.conf.json'])
def test_document_preview_allows_the_compact_acceptance_window(config_name):
    root = Path(__file__).resolve().parents[3]
    config = json.loads((root / 'apps/agent-desktop/src-tauri' / config_name).read_text())
    window = config['app']['windows'][0]
    assert window['minWidth'] <= 900
    assert window['minHeight'] <= 500
