import json
from pathlib import Path
import pytest

from agent.protocol import BUILD_ID, CORE_VERSION, PROTOCOL_VERSION
from agent.storage import SCHEMA_VERSION


def test_unified_agent_has_separate_app_and_protocol_identity():
    root = Path(__file__).resolve().parents[3]
    config = json.loads((root / 'apps/agent-desktop/src-tauri/tauri.conf.json').read_text())
    assert config['identifier'] == 'io.github.zcchou.agent.airworthiness'
    assert config['productName'] == 'agent'
    assert config['plugins']['updater']['pubkey'] == (root / 'apps/agent-desktop/src-tauri/updater-public-key.txt').read_text().strip()
    credentials = (root / 'apps/agent-desktop/src-tauri/src/credentials.rs').read_text()
    assert 'io.github.zcchou.agent.airworthiness Model Providers' in credentials
    assert (CORE_VERSION, PROTOCOL_VERSION, SCHEMA_VERSION, BUILD_ID) == (
        '0.16.0-preview.1', 23, 30, 'schema30-agent-unified-v1')


@pytest.mark.parametrize('config_name', ['tauri.document-preview.conf.json', 'tauri.conf.json'])
def test_document_preview_allows_the_compact_acceptance_window(config_name):
    root = Path(__file__).resolve().parents[3]
    config = json.loads((root / 'apps/agent-desktop/src-tauri' / config_name).read_text())
    window = config['app']['windows'][0]
    assert window['minWidth'] <= 900
    assert window['minHeight'] <= 500
