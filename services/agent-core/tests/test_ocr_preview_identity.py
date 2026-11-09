import json
from pathlib import Path

from agent.protocol import BUILD_ID, CORE_VERSION, PROTOCOL_VERSION
from agent.storage import SCHEMA_VERSION


def test_ocr_preview_has_separate_app_and_protocol_identity():
    root = Path(__file__).resolve().parents[3]
    config = json.loads((root / 'apps/agent-desktop/src-tauri/tauri.conf.json').read_text())
    assert config['identifier'] == 'io.github.zcchou.agent.ocrpreview'
    assert config['productName'] == 'agent OCR Preview'
    assert (CORE_VERSION, PROTOCOL_VERSION, SCHEMA_VERSION, BUILD_ID) == (
        '0.15.0', 19, 26, 'schema26-auto-ocr-v1')
