"""Offline provenance checks and explicit public binary publication. No source Git pushes."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

PUBLIC_REPO = 'ZCchou/agent'
SOURCE_REPO = 'ZCchou/agent'
CARGO_PATH = 'apps/agent-desktop/src-tauri/Cargo.toml'
VERIFIER = Path(__file__).with_name('verify-updater-signature.cjs')


def command(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(list(map(str, args)), cwd=cwd, capture_output=True, text=True, encoding='utf-8')
    if result.returncode:
        # Command arguments may contain API payloads; never echo credentials or arbitrary output.
        raise ValueError(f'{args[0]} failed (exit {result.returncode})')
    return result.stdout.rstrip("\r\n")


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def normalize_source(root: Path, initial_commit: str, allow_dirty: bool = False) -> dict:
    def git(*args):
        return command('git', '-C', root, *args)
    if git('rev-parse', 'HEAD') != initial_commit:
        raise ValueError('Release HEAD drifted from the initial commit')
    # NUL-delimited status avoids whitespace/quoting ambiguities and includes untracked files.
    state = git('status', '--porcelain=v1', '-z', '--untracked-files=all')
    if allow_dirty:
        return {'dirty': bool(state), 'normalized': []}
    normalized = []
    if state:
        entries = state.rstrip('\0').split('\0')
        if entries != [' M ' + CARGO_PATH]:
            raise ValueError('Unexpected source changes during release; nothing was restored')
        expected = git('rev-parse', f'{initial_commit}:{CARGO_PATH}')
        actual = git('hash-object', f'--path={CARGO_PATH}', '--', CARGO_PATH)
        if expected != actual:
            raise ValueError('Semantic source changes during release; nothing was restored')
        git('restore', f'--source={initial_commit}', '--worktree', '--', CARGO_PATH)
        normalized.append(CARGO_PATH)
    if git('rev-parse', 'HEAD') != initial_commit or git('status', '--porcelain=v1', '-z', '--untracked-files=all'):
        raise ValueError('Source state changed during release verification')
    return {'dirty': False, 'normalized': normalized}


def release_contract(manifest: dict) -> tuple[str, str, str]:
    version, tag, channel = (manifest.get(k, '') for k in ('version', 'release_tag', 'update_channel'))
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Expected numeric application version')
    valid_tag = tag == f'v{version}' if channel == 'stable' else bool(re.fullmatch(rf'v{re.escape(version)}-preview\.[1-9]\d*', tag)) if channel == 'preview' else False
    if not valid_tag:
        raise ValueError('Release tag, version and update channel disagree')
    if not re.fullmatch(r'[0-9a-f]{40}', manifest.get('git_commit', '')) or manifest.get('dirty_worktree') is not False:
        raise ValueError('Public releases require clean immutable source provenance')
    if manifest.get('updater_signed') is not True:
        raise ValueError('Missing verified updater signature provenance')
    if manifest.get('build_gates') != {'tests_passed': True, 'installer_smoke_passed': True}:
        raise ValueError('Public releases require all authoritative tests and installer smoke gates')
    if manifest.get('platform') != 'windows-x86_64' or manifest.get('product') != 'agent':
        raise ValueError('Unexpected release product/platform')
    try:
        published = dt.datetime.fromisoformat(manifest['built_at_utc'].replace('Z', '+00:00'))
        if published.utcoffset() != dt.timedelta(0):
            raise ValueError('Expected UTC build time')
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('Invalid UTC build time') from error
    return version, tag, channel


def file_record(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or not path.stat().st_size:
        raise ValueError(f'Missing, empty or linked artifact: {path.name}')
    return {'name': path.name, 'bytes': path.stat().st_size, 'sha256': sha256(path)}


def prepare_public(directory: Path, config: Path, notes: str = '') -> Path:
    if not (os.environ.get('TAURI_SIGNING_PRIVATE_KEY') or os.environ.get('TAURI_SIGNING_PRIVATE_KEY_PATH')):
        raise ValueError('Missing updater signing environment for signed feed generation')
    manifest = read_json(directory / 'release-manifest.json')
    version, tag, channel = release_contract(manifest)
    name = f'agent-{version}-windows-x86_64-setup.exe'
    installer, signature = directory / name, directory / (name + '.sig')
    record = file_record(installer)
    if [f for f in manifest['files'] if f['name'] == name] != [record]:
        raise ValueError('Installer differs from authoritative build provenance')
    if [f for f in manifest['files'] if f['name'] == signature.name] != [file_record(signature)]:
        raise ValueError('Signature differs from authoritative build provenance')
    verification = json.loads(command('node', VERIFIER, installer, signature, config))
    if verification.get('verified') is not True:
        raise ValueError('Signature verification did not succeed')
    feed = {'version': version, 'notes': notes, 'pub_date': manifest['built_at_utc'], 'platforms': {
        'windows-x86_64': {'url': f'https://github.com/{PUBLIC_REPO}/releases/download/{tag}/{name}',
                           'signature': signature.read_text(encoding='utf-8-sig').strip()}}}
    public = directory / 'public'
    public.mkdir(exist_ok=True)
    expected = {name, name + '.sig', 'latest.json', 'latest.json.sig', 'release-manifest.json', 'SHA256SUMS.txt'}
    if any(p.name not in expected or not p.is_file() or p.is_symlink() for p in public.iterdir()):
        raise ValueError('Unexpected public artifact; use an empty public output directory')
    for path in (installer, signature):
        shutil.copyfile(path, public / path.name)
    write_json(public / 'latest.json', feed)
    tauri_cli = Path(__file__).resolve().parents[1] / 'apps/agent-desktop/node_modules/@tauri-apps/cli/tauri.js'
    command('node', tauri_cli, 'signer', 'sign', public / 'latest.json')
    command('node', VERIFIER, public / 'latest.json', public / 'latest.json.sig', config)
    published_manifest = {key: manifest[key] for key in ('product', 'version', 'platform', 'git_commit', 'dirty_worktree', 'signed', 'updater_signed', 'release_tag', 'update_channel', 'built_at_utc', 'build_gates')}
    published_manifest['updater_key_id'] = verification['key_id']
    if 'build_provenance' in manifest:
        published_manifest['build_provenance'] = manifest['build_provenance']
    published_manifest['files'] = [file_record(public / filename) for filename in (name, name + '.sig', 'latest.json', 'latest.json.sig')]
    write_json(public / 'release-manifest.json', published_manifest)
    (public / 'SHA256SUMS.txt').write_text(''.join(f'{sha256(public / filename)}  {filename}\n' for filename in (name, name + '.sig', 'latest.json', 'latest.json.sig', 'release-manifest.json')), encoding='ascii')
    return public


def validate_public(directory: Path, config: Path) -> tuple[dict, Path]:
    manifest = read_json(directory / 'release-manifest.json')
    version, tag, _ = release_contract(manifest)
    public = directory / 'public'
    name = f'agent-{version}-windows-x86_64-setup.exe'
    expected = {name, name + '.sig', 'latest.json', 'latest.json.sig', 'release-manifest.json', 'SHA256SUMS.txt'}
    if {p.name for p in public.iterdir()} != expected or any(p.is_symlink() or not p.is_file() for p in public.iterdir()):
        raise ValueError('Unexpected or missing public release assets')
    published = read_json(public / 'release-manifest.json')
    public_keys = {'product', 'version', 'platform', 'git_commit', 'dirty_worktree', 'signed', 'updater_signed', 'release_tag', 'update_channel', 'built_at_utc', 'build_gates', 'updater_key_id', 'files'}
    if 'build_provenance' in manifest:
        public_keys.add('build_provenance')
    if set(published) != public_keys:
        raise ValueError('Public manifest contains unexpected or missing provenance fields')
    release_contract(published)
    for key, value in published.items():
        if key not in ('files', 'updater_key_id') and manifest.get(key) != value:
            raise ValueError('Public provenance differs from authoritative build')
    if [f for f in manifest['files'] if f['name'] == name] != [file_record(public / name)]:
        raise ValueError('Public installer differs from authoritative build')
    if [f for f in manifest['files'] if f['name'] == name + '.sig'] != [file_record(public / (name + '.sig'))]:
        raise ValueError('Public signature differs from authoritative build')
    records = [file_record(public / filename) for filename in (name, name + '.sig', 'latest.json', 'latest.json.sig')]
    if published['files'] != records:
        raise ValueError('Public artifact hash/size mismatch')
    checksum = ''.join(f'{sha256(public / filename)}  {filename}\n' for filename in (name, name + '.sig', 'latest.json', 'latest.json.sig', 'release-manifest.json'))
    if (public / 'SHA256SUMS.txt').read_text(encoding='ascii') != checksum:
        raise ValueError('Public checksums mismatch')
    verification = json.loads(command('node', VERIFIER, public / name, public / (name + '.sig'), config))
    if not verification.get('verified') or verification.get('key_id') != published['updater_key_id']:
        raise ValueError('Public updater signature metadata mismatch')
    command('node', VERIFIER, public / 'latest.json', public / 'latest.json.sig', config)
    feed = read_json(public / 'latest.json')
    platform = {'url': f'https://github.com/{PUBLIC_REPO}/releases/download/{tag}/{name}', 'signature': (public / (name + '.sig')).read_text(encoding='utf-8-sig').strip()}
    if set(feed) != {'version', 'notes', 'pub_date', 'platforms'} or feed['version'] != version or feed['pub_date'] != manifest['built_at_utc'] or feed['platforms'] != {'windows-x86_64': platform}:
        raise ValueError('Updater feed does not match verified package')
    return manifest, public


class GithubClient:
    """The only network boundary. gh reads its existing auth store; no token extraction."""
    def __init__(self, tag: str):
        self.tag = tag

    def api(self, method: str, path: str, payload: dict | None = None, missing_ok: bool = False):
        args = ['gh', 'api', '--method', method, path, '-H', 'Accept: application/vnd.github+json', '-H', 'X-GitHub-Api-Version: 2022-11-28']
        if payload is not None:
            args += ['--input', '-']
        result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, encoding='utf-8')
        if result.returncode:
            if missing_ok and 'HTTP 404' in result.stderr:
                return None
            raise ValueError(f'GitHub {method} request failed (exit {result.returncode})')
        return json.loads(result.stdout) if result.stdout.strip() else None

    def upload(self, release_id: int, path: Path) -> None:
        command('gh', 'release', 'upload', self.tag, str(path), '--repo', PUBLIC_REPO)


def publish_public(directory: Path, config: Path, workflow_run: int, github=None) -> dict:
    manifest, public = validate_public(directory, config)
    version, tag, channel = release_contract(manifest)
    github = github or GithubClient(tag)
    provenance = manifest.get('build_provenance')
    if provenance != {'repository': SOURCE_REPO, 'workflow_run_id': str(workflow_run), 'commit': manifest['git_commit']}:
        raise ValueError('Missing or mismatched authoritative workflow provenance')
    if github.api('GET', f'repos/{SOURCE_REPO}').get('private') is not False:
        raise ValueError('Source repository must be public')
    if github.api('GET', f'repos/{PUBLIC_REPO}').get('private') is not False:
        raise ValueError('Distribution repository must be public')
    run = github.api('GET', f'repos/{SOURCE_REPO}/actions/runs/{workflow_run}')
    if (run.get('id') != workflow_run or run.get('status') != 'completed' or run.get('conclusion') != 'success'
            or run.get('head_sha') != manifest['git_commit'] or run.get('path') != '.github/workflows/windows-release.yml'
            or run.get('event') not in ('workflow_dispatch', 'push')):
        raise ValueError('Authoritative release workflow did not succeed for this commit')
    if github.api('GET', f'repos/{PUBLIC_REPO}/releases/tags/{tag}', missing_ok=True) is not None:
        raise ValueError('Public release tag already exists; immutable assets must never be overwritten')
    if github.api('GET', f'repos/{PUBLIC_REPO}/git/ref/tags/{tag}', missing_ok=True) is not None:
        raise ValueError('Public Git tag already exists; immutable tags must never be reused')
    feed_path = f'repos/{PUBLIC_REPO}/contents/updates/{channel}.json'
    head = github.api('GET', f'repos/{PUBLIC_REPO}/git/ref/heads/main')['object']['sha']
    base_tree = github.api('GET', f'repos/{PUBLIC_REPO}/git/commits/{head}')['tree']['sha']
    previous = github.api('GET', feed_path + '?ref=' + head, missing_ok=True)
    if previous:
        old_feed = json.loads(base64.b64decode(previous['content']).decode('utf-8-sig'))
        old_version = old_feed.get('version', '')
        if not re.fullmatch(r'\d+\.\d+\.\d+', old_version):
            raise ValueError('Existing channel feed has an invalid version; inspect it before publication')
        if tuple(map(int, old_version.split('.'))) >= tuple(map(int, version.split('.'))):
            raise ValueError('Channel feed can advance only to a greater application version')
    release = github.api('POST', f'repos/{PUBLIC_REPO}/releases', {
        'tag_name': tag, 'target_commitish': manifest['git_commit'], 'name': f'agent {tag}',
        'body': read_json(public / 'latest.json')['notes'], 'draft': True, 'prerelease': channel == 'preview',
        'make_latest': 'false'})
    for path in sorted(public.iterdir()):
        github.upload(release['id'], path)

    def verify_assets(remote):
        assets = remote.get('assets', [])
        paths = {path.name: path for path in public.iterdir()}
        if len(assets) != len(paths) or {item['name'] for item in assets} != set(paths):
            raise ValueError('Remote release asset set differs from the verified public package')
        for item in assets:
            path = paths[item['name']]
            if item.get('state') != 'uploaded' or item.get('size') != path.stat().st_size or item.get('digest') != 'sha256:' + sha256(path):
                raise ValueError('GitHub release asset digest/size verification failed')

    verify_assets(github.api('GET', f'repos/{PUBLIC_REPO}/releases/{release["id"]}'))
    validate_public(directory, config)
    github.api('PATCH', f'repos/{PUBLIC_REPO}/releases/{release["id"]}', {
        'draft': False, 'prerelease': channel == 'preview', 'make_latest': 'false' if channel == 'preview' else 'true'})
    published = github.api('GET', f'repos/{PUBLIC_REPO}/releases/{release["id"]}')
    if published.get('draft') is not False or published.get('prerelease') != (channel == 'preview'):
        raise ValueError('Package release was not published with the selected channel')
    verify_assets(published)
    validate_public(directory, config)
    # Both files become visible in one public Git commit. The parent is public
    # history only; force=false rejects a concurrent branch change atomically.
    entries = []
    for filename, suffix in (('latest.json', ''), ('latest.json.sig', '.sig')):
        blob = github.api('POST', f'repos/{PUBLIC_REPO}/git/blobs', {
            'encoding': 'base64', 'content': base64.b64encode((public / filename).read_bytes()).decode('ascii')})
        entries.append({'path': f'updates/{channel}.json{suffix}', 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
    tree = github.api('POST', f'repos/{PUBLIC_REPO}/git/trees', {'base_tree': base_tree, 'tree': entries})
    commit = github.api('POST', f'repos/{PUBLIC_REPO}/git/commits', {
        'message': f'Update {channel} to {tag}', 'tree': tree['sha'], 'parents': [head]})
    github.api('PATCH', f'repos/{PUBLIC_REPO}/git/refs/heads/main', {'sha': commit['sha'], 'force': False})
    for filename, suffix in (('latest.json', ''), ('latest.json.sig', '.sig')):
        confirmed = github.api('GET', feed_path + suffix + '?ref=main')
        if base64.b64decode(confirmed['content']) != (public / filename).read_bytes():
            raise ValueError('Published channel feed/signature read-back differs from the verified package')
    return {'published': True, 'tag': tag, 'channel': channel, 'repository': PUBLIC_REPO}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='action', required=True)
    normalizer = subs.add_parser('normalize-source')
    normalizer.add_argument('--root', type=Path, required=True)
    normalizer.add_argument('--initial-commit', required=True)
    normalizer.add_argument('--allow-dirty', action='store_true')
    for action in ('prepare-public', 'validate-public', 'publish-public'):
        sub = subs.add_parser(action)
        sub.add_argument('--directory', type=Path, required=True)
        sub.add_argument('--config', type=Path, required=True)
        if action == 'prepare-public':
            sub.add_argument('--notes-file', type=Path)
        if action == 'publish-public':
            sub.add_argument('--workflow-run', type=int, required=True)
    args = parser.parse_args()
    if args.action == 'normalize-source':
        print(json.dumps(normalize_source(args.root.resolve(), args.initial_commit, args.allow_dirty)))
    elif args.action == 'prepare-public':
        print(prepare_public(args.directory.resolve(), args.config.resolve(), args.notes_file.read_text(encoding='utf-8-sig') if args.notes_file else ''))
    elif args.action == 'publish-public':
        print(json.dumps(publish_public(args.directory.resolve(), args.config.resolve(), args.workflow_run)))
    else:
        manifest, _ = validate_public(args.directory.resolve(), args.config.resolve())
        print(json.dumps({'verified': True, 'tag': manifest['release_tag']}))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        print(f'Release validation failed: {error}', file=sys.stderr)
        sys.exit(1)
