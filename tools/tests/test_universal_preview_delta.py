import json
from pathlib import Path
import zipfile

import pytest

from test_preview_delta import apply, fixture, record


def combined_fixture(tmp_path):
    archives, profiles = [], {}
    for kind in ('ocr', 'document'):
        folder = tmp_path / kind
        folder.mkdir()
        release, old, new, target, _ = fixture(folder)
        baseline = json.loads((old / 'delivery-manifest.json').read_text())
        current = json.loads((new / 'delivery-manifest.json').read_text())
        identity = f'io.github.zcchou.agent.{kind}preview'
        baseline['application_id'] = current['application_id'] = identity
        baseline['version'] = f'0.15.0-{kind}-preview.1'
        current['version'] = f'0.15.0-{kind}-preview.2'
        for name in ('agent-desktop.exe', 'agent_desktop_lib.dll'):
            (old / name).write_bytes(('old-' + kind + '-' + name).encode())
            (new / name).write_bytes(('new-' + kind + '-' + name).encode())
            (target / name).write_bytes((old / name).read_bytes())
            baseline['files'][name], current['files'][name] = record(old / name), record(new / name)
        (new / 'delivery-manifest.json').write_text(json.dumps(current))
        archive = release.build_delta(new, baseline, folder / 'profile.zip')
        archives.append(archive)
        profiles[kind] = (release, old, new, target, current)
    output = tmp_path / 'universal.zip'
    release.combine_deltas(archives, output)
    bundle = tmp_path / 'universal'
    with zipfile.ZipFile(output) as package:
        assert len([name for name in package.namelist() if name.endswith('/agent-core.exe')]) == 1
        assert 'shared/agent-core.exe' in package.namelist()
        package.extractall(bundle)
    return bundle, profiles, archives


@pytest.mark.parametrize('kind', ['ocr', 'document'])
def test_universal_detects_profile_updates_and_is_idempotent(tmp_path, kind):
    bundle, profiles, _ = combined_fixture(tmp_path)
    release, old, new, target, current = profiles[kind]
    (target / 'agent.db').write_bytes(b'keep user data')
    result = apply(bundle, target)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert current['application_id'].encode() in result.stdout
    for name in release.APPLICATION:
        assert (target / name).read_bytes() == (new / name).read_bytes()
    assert (target / 'agent.db').read_bytes() == b'keep user data'
    again = apply(bundle, target)
    assert again.returncode == 0 and b'ALREADY_UPDATED' in again.stdout


@pytest.mark.parametrize('kind', ['ocr', 'document'])
def test_universal_detects_interrupted_profile_with_new_host(tmp_path, kind):
    bundle, profiles, _ = combined_fixture(tmp_path)
    release, old, new, target, current = profiles[kind]
    previous = target / '.agent-app-update' / ('b' * 32) / 'previous'
    previous.mkdir(parents=True)
    for name in release.APPLICATION:
        (previous / name).write_bytes((old / name).read_bytes())
    (previous.parents[1] / 'owner.txt').write_text('agent-preview-app-update-v1')
    (previous.parents[1] / 'pending.json').write_text(json.dumps({'id': 'b' * 32, 'version': current['version']}))
    (target / 'agent-desktop.exe').write_bytes((new / 'agent-desktop.exe').read_bytes())
    result = apply(bundle, target)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert b'RECOVERED_PREVIOUS_APPLICATION' in result.stdout
    for name in release.APPLICATION:
        assert (target / name).read_bytes() == (new / name).read_bytes()


@pytest.mark.parametrize('fault', ['unknown_host', 'shared_core'])
def test_universal_rejects_invalid_input_without_mutation(tmp_path, fault):
    bundle, profiles, _ = combined_fixture(tmp_path)
    release, old, new, target, current = profiles['document']
    if fault == 'unknown_host':
        (target / 'agent-desktop.exe').write_bytes(b'unknown application')
    else:
        (bundle / 'shared/agent-core.exe').write_bytes(b'corrupt update')
    before = {name: (target / name).read_bytes() for name in release.APPLICATION}
    assert apply(bundle, target).returncode != 0
    assert {name: (target / name).read_bytes() for name in release.APPLICATION} == before


def test_universal_builder_rejects_different_shared_core(tmp_path):
    bundle, profiles, archives = combined_fixture(tmp_path)
    release, old, new, target, current = profiles['ocr']
    (new / 'agent-core.exe').write_bytes(b'different core')
    current['files']['agent-core.exe'] = record(new / 'agent-core.exe')
    (new / 'delivery-manifest.json').write_text(json.dumps(current))
    baseline = json.loads((bundle / 'profiles/ocr/update.json').read_text())
    baseline = {'version':baseline['base_version'], 'application_id':current['application_id'],
                'files':{**baseline['base_files'], **current['files']}}
    baseline['files']['agent-core.exe'] = record(old / 'agent-core.exe')
    revised = release.build_delta(new, baseline, tmp_path / 'revised.zip')
    with pytest.raises(ValueError, match='shared Core'):
        release.combine_deltas([revised, archives[1]], tmp_path / 'invalid.zip')
