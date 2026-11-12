"""Offline behavioral checks for release tooling; no live services or signing credentials."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
HELPER = TOOLS / 'release_windows.py'
VERIFIER = TOOLS / 'verify-updater-signature.cjs'
CARGO = 'apps/agent-desktop/src-tauri/Cargo.toml'


def run(*args, cwd=None, check=True, env=None):
    result = subprocess.run(list(map(str, args)), cwd=cwd, capture_output=True, text=True, env=env)
    if check and result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result


def cli(*args, check=True, env=None):
    return run(sys.executable, HELPER, *args, check=check, env=env)


def write_json(path, obj):
    path.write_text(json.dumps(obj), encoding='utf-8')


class SourceStateTests(unittest.TestCase):
    def test_rag_builder_accepts_external_build_and_cuda_license_paths(self):
        script = TOOLS / 'build-rag-runtime.ps1'
        result = run('powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
                     '(Get-Command $env:AGENT_RAG_TEST_SCRIPT).Parameters.Keys | ConvertTo-Json',
                     env={**os.environ, 'AGENT_RAG_TEST_SCRIPT': str(script)})
        parameters = json.loads(result.stdout)
        self.assertIn('BuildDirectory', parameters)
        self.assertIn('CudaLicensePath', parameters)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent-release-git-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        run('git', 'init', '-q', self.root)
        run('git', '-C', self.root, 'config', 'user.email', 'fixture@example.invalid')
        run('git', '-C', self.root, 'config', 'user.name', 'Fixture')
        run('git', '-C', self.root, 'config', 'core.autocrlf', 'false')
        (self.root / CARGO).parent.mkdir(parents=True)
        (self.root / CARGO).write_bytes(b'[package]\r\nversion = "1.0.0"\r\n')
        (self.root / '.gitattributes').write_text('*.toml text eol=crlf\n')
        (self.root / 'other.txt').write_text('original\n')
        run('git', '-C', self.root, 'add', '.')
        run('git', '-C', self.root, 'commit', '-qm', 'fixture')
        run('git', '-C', self.root, 'restore', '--worktree', '--', CARGO)
        self.commit = run('git', '-C', self.root, 'rev-parse', 'HEAD').stdout.strip()

    def normalize(self, *extra, check=True):
        return cli('normalize-source', '--root', self.root, '--initial-commit', self.commit, *extra, check=check)

    def test_clean_tree_keeps_commit(self):
        self.assertEqual(json.loads(self.normalize().stdout), {'dirty': False, 'normalized': []})
        self.assertEqual(run('git', '-C', self.root, 'rev-parse', 'HEAD').stdout.strip(), self.commit)

    def test_only_canonical_identical_cargo_line_endings_are_restored(self):
        original = (self.root / CARGO).read_bytes()
        (self.root / CARGO).write_bytes(original.replace(b'\r\n', b'\n'))
        state = json.loads(self.normalize().stdout)
        self.assertFalse(state['dirty'])
        self.assertEqual(state['normalized'], [CARGO])
        self.assertEqual((self.root / CARGO).read_bytes(), original)

    def test_semantic_change_is_rejected_and_preserved(self):
        (self.root / CARGO).write_text('[package]\nversion = "9.0.0"\n')
        result = self.normalize(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('source changes', result.stderr)
        self.assertIn('9.0.0', (self.root / CARGO).read_text())

    def test_unrelated_and_untracked_changes_are_rejected_without_restoring(self):
        for path in ['other.txt', 'untracked.txt']:
            with self.subTest(path=path):
                (self.root / path).write_text('changed')
                self.assertNotEqual(self.normalize(check=False).returncode, 0)
                self.assertEqual((self.root / path).read_text(), 'changed')
                if path == 'other.txt':
                    run('git', '-C', self.root, 'restore', '--worktree', '--', path)

    def test_staged_change_is_rejected(self):
        (self.root / CARGO).write_text('[package]\nversion = "2"\n')
        run('git', '-C', self.root, 'add', CARGO)
        self.assertNotEqual(self.normalize(check=False).returncode, 0)

    def test_head_drift_is_rejected_even_when_allow_dirty(self):
        run('git', '-C', self.root, 'commit', '--allow-empty', '-qm', 'new head')
        self.assertNotEqual(self.normalize('--allow-dirty', check=False).returncode, 0)

    def test_explicit_allow_dirty_preserves_all_local_changes(self):
        (self.root / 'other.txt').write_text('local')
        self.assertTrue(json.loads(self.normalize('--allow-dirty').stdout)['dirty'])
        self.assertEqual((self.root / 'other.txt').read_text(), 'local')


class SignedArtifactFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent-release-artifacts-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.installer = self.root / 'agent-0.14.1-windows-x86_64-setup.exe'
        self.installer.write_bytes(b'synthetic installer, never executable')
        self.tauri = TOOLS.parent / 'apps/agent-desktop/node_modules/@tauri-apps/cli/tauri.js'
        self.synthetic_key = self.root / 'temporary-synthetic-signing.key'
        self.signing_env = {k: v for k, v in os.environ.items() if not k.startswith('TAURI_SIGNING_')}
        run('node', self.tauri, 'signer', 'generate', '--ci', '-w', self.synthetic_key, '-p', '', env=self.signing_env)
        self.signing_env.update(TAURI_SIGNING_PRIVATE_KEY_PATH=str(self.synthetic_key), TAURI_SIGNING_PRIVATE_KEY_PASSWORD='')
        run('node', self.tauri, 'signer', 'sign', self.installer, env=self.signing_env)
        self.signature = self.installer.with_suffix('.exe.sig')
        self.keys = {'pubkey': Path(str(self.synthetic_key) + '.pub').read_text().strip(), 'signature': self.signature.read_text().strip()}
        self.signature.write_text(self.keys['signature'] + '\n')
        self.config = self.root / 'tauri.json'
        write_json(self.config, {'plugins': {'updater': {'pubkey': self.keys['pubkey']}}})
        self.manifest = {
            'product': 'agent', 'version': '0.14.1', 'platform': 'windows-x86_64',
            'git_commit': 'a' * 40, 'dirty_worktree': False, 'signed': False,
            'updater_signed': True, 'release_tag': 'v0.14.1-preview.1', 'update_channel': 'preview',
            'built_at_utc': '2026-10-26T00:00:00Z',
            'build_gates': {'tests_passed': True, 'installer_smoke_passed': True},
            'files': [{'name': self.installer.name, 'bytes': self.installer.stat().st_size,
                       'sha256': hashlib.sha256(self.installer.read_bytes()).hexdigest()}],
        }
        self.manifest['files'].append({'name': self.signature.name, 'bytes': self.signature.stat().st_size, 'sha256': hashlib.sha256(self.signature.read_bytes()).hexdigest()})
        write_json(self.root / 'release-manifest.json', self.manifest)
        (self.root / 'agent-core.exe').write_bytes(b'private diagnostic')

    def prepare(self, check=True):
        return cli('prepare-public', '--directory', self.root, '--config', self.config, check=check, env=self.signing_env)



class SignedArtifactTests(SignedArtifactFixture):
    def test_signature_verifies_real_bytes_and_rejects_tampering(self):
        run('node', VERIFIER, self.installer, self.signature, self.config)
        self.installer.write_bytes(b'tampered')
        self.assertNotEqual(run('node', VERIFIER, self.installer, self.signature, self.config, check=False).returncode, 0)

    def test_invalid_signature_comment_and_wrong_public_key_are_rejected(self):
        envelope = base64.b64decode(self.keys['signature']).decode().replace('trusted comment: timestamp:', 'trusted comment: tampered timestamp:', 1)
        self.signature.write_text(base64.b64encode(envelope.encode()).decode())
        self.assertNotEqual(run('node', VERIFIER, self.installer, self.signature, self.config, check=False).returncode, 0)
        self.assertNotEqual(self.prepare(check=False).returncode, 0)
        self.signature.write_text(self.keys['signature'])
        key = bytearray(base64.b64decode(self.keys['pubkey']))
        key[-5] ^= 1
        write_json(self.config, {'plugins': {'updater': {'pubkey': base64.b64encode(key).decode()}}})
        self.assertNotEqual(self.prepare(check=False).returncode, 0)

    def test_prepare_emits_standard_feed_and_only_public_assets(self):
        self.prepare()
        public = self.root / 'public'
        self.assertEqual({p.name for p in public.iterdir()}, {self.installer.name, self.signature.name, 'latest.json', 'latest.json.sig', 'release-manifest.json', 'SHA256SUMS.txt'})
        feed = json.loads((public / 'latest.json').read_text())
        self.assertEqual(feed['version'], '0.14.1')
        self.assertEqual(feed['platforms']['windows-x86_64']['signature'], self.keys['signature'])
        self.assertEqual(feed['platforms']['windows-x86_64']['url'], 'https://github.com/ZCchou/agent/releases/download/v0.14.1-preview.1/' + self.installer.name)
        manifest = json.loads((public / 'release-manifest.json').read_text())
        self.assertEqual([f['name'] for f in manifest['files']], [self.installer.name, self.signature.name, 'latest.json', 'latest.json.sig'])
        cli('validate-public', '--directory', self.root, '--config', self.config)

    def test_provenance_hash_missing_signature_and_skipped_gates_fail_closed(self):
        for field, value in [('dirty_worktree', True), ('updater_signed', False), ('git_commit', ''), ('build_gates', {'tests_passed': False, 'installer_smoke_passed': True})]:
            with self.subTest(field=field):
                changed = dict(self.manifest, **{field: value})
                write_json(self.root / 'release-manifest.json', changed)
                self.assertNotEqual(self.prepare(check=False).returncode, 0)
        write_json(self.root / 'release-manifest.json', self.manifest)
        self.installer.write_bytes(b'changed')
        self.assertNotEqual(self.prepare(check=False).returncode, 0)
        self.signature.unlink()
        self.assertNotEqual(self.prepare(check=False).returncode, 0)

    def test_signature_must_match_authoritative_hash_even_if_still_cryptographically_valid(self):
        self.signature.write_text(self.keys['signature'])
        self.assertNotEqual(self.prepare(check=False).returncode, 0)

    def test_stable_preview_tag_confusion_is_rejected(self):
        self.manifest['update_channel'] = 'stable'
        write_json(self.root / 'release-manifest.json', self.manifest)
        self.assertNotEqual(self.prepare(check=False).returncode, 0)

    def test_feed_signature_verifies_and_tampered_feed_cannot_verify(self):
        self.prepare()
        feed = self.root / 'public/latest.json'
        signature = self.root / 'public/latest.json.sig'
        run('node', VERIFIER, feed, signature, self.config)
        value = json.loads(feed.read_text())
        value['version'] = '999.0.0'
        write_json(feed, value)
        self.assertNotEqual(run('node', VERIFIER, feed, signature, self.config, check=False).returncode, 0)

    def test_missing_feed_signing_environment_fails_closed(self):
        safe_env = {k: v for k, v in os.environ.items() if not k.startswith('TAURI_SIGNING_')}
        self.assertNotEqual(cli('prepare-public', '--directory', self.root, '--config', self.config, env=safe_env, check=False).returncode, 0)

    def test_private_manifest_fields_cannot_be_added_to_public_manifest(self):
        self.prepare()
        self.manifest['private_notes'] = 'private diagnostic note'
        write_json(self.root / 'release-manifest.json', self.manifest)
        public = self.root / 'public'
        manifest = json.loads((public / 'release-manifest.json').read_text())
        manifest['private_notes'] = self.manifest['private_notes']
        write_json(public / 'release-manifest.json', manifest)
        (public / 'SHA256SUMS.txt').write_text(''.join(
            hashlib.sha256((public / name).read_bytes()).hexdigest() + '  ' + name + '\n'
            for name in (self.installer.name, self.signature.name, 'latest.json', 'latest.json.sig', 'release-manifest.json')))
        self.assertNotEqual(cli('validate-public', '--directory', self.root, '--config', self.config, check=False).returncode, 0)

    def test_unexpected_public_asset_or_feed_mutation_is_rejected(self):
        self.prepare()
        public = self.root / 'public'
        (public / 'secret.log').write_text('never publish')
        self.assertNotEqual(cli('validate-public', '--directory', self.root, '--config', self.config, check=False).returncode, 0)
        (public / 'secret.log').unlink()
        feed = json.loads((public / 'latest.json').read_text())
        feed['platforms']['windows-x86_64']['url'] = 'https://attacker.invalid/setup.exe'
        write_json(public / 'latest.json', feed)
        self.assertNotEqual(cli('validate-public', '--directory', self.root, '--config', self.config, check=False).returncode, 0)

# The network boundary is replaced; all package, signature and feed checks remain real.
class GithubFixture:
    def __init__(self, commit):
        self.commit = commit
        self.releases = []
        self.feeds = {'updates/stable.json': b'{"version":"0.14.0"}', 'updates/stable.json.sig': b'older stable signature'}
        self.bad_digest = False
        self.failed_publish = False
        self.run_success = True
        self.source_private = False
        self.blobs = {}
        self.tree = []
        self.ref_failure = False
        self.corrupt_package = False
        self.tag_exists = False

    def api(self, method, path, payload=None, missing_ok=False):
        if path == 'repos/ZCchou/agent':
            return {'private': self.source_private}
        if '/actions/runs/' in path:
            return {'id': 123, 'status': 'completed', 'conclusion': 'success' if self.run_success else 'failure',
                    'head_sha': self.commit, 'path': '.github/workflows/windows-release.yml', 'event': 'workflow_dispatch'}
        if '/git/ref/tags/' in path:
            return {'object': {'sha': 'existing-tag'}} if self.tag_exists else None
        if '/releases/tags/' in path:
            return self.releases[0] if self.releases else None
        if path.endswith('/releases') and method == 'POST':
            item = dict(payload, id=17, assets=[])
            self.releases.append(item)
            return item
        if path.endswith('/releases/17'):
            if method == 'PATCH':
                if self.failed_publish:
                    raise ValueError('remote publication failed')
                self.releases[0].update(payload)
            return self.releases[0]
        if path.endswith('/git/ref/heads/main'):
            return {'object': {'sha': 'public-head'}}
        if path.endswith('/git/commits/public-head'):
            return {'tree': {'sha': 'public-tree'}}
        if path.endswith('/git/blobs'):
            sha = 'blob-' + str(len(self.blobs))
            self.blobs[sha] = base64.b64decode(payload['content'])
            return {'sha': sha}
        if path.endswith('/git/trees'):
            self.tree = payload['tree']
            return {'sha': 'new-tree'}
        if path.endswith('/git/commits'):
            if payload['parents'] != ['public-head']:
                raise AssertionError('Commit must descend only from public history')
            return {'sha': 'new-public-commit'}
        if path.endswith('/git/refs/heads/main'):
            if payload.get('force') is not False:
                raise AssertionError('Concurrent branch changes must not be forced')
            if self.ref_failure:
                raise ValueError('Concurrent public branch update')
            if self.releases[0]['draft']:
                raise AssertionError('Feed advanced before package publication')
            for item in self.tree:
                self.feeds[item['path']] = self.blobs[item['sha']]
            return {'object': {'sha': 'new-public-commit'}}
        if '/contents/' in path:
            name = path.split('/contents/')[1].split('?')[0]
            if method == 'GET':
                return {'sha': 'previous', 'content': base64.b64encode(self.feeds[name]).decode()} if name in self.feeds else None
            raise AssertionError('Channel JSON and signature must advance atomically via Git Data')
        raise AssertionError((method, path, payload))

    def upload(self, release_id, path):
        if self.corrupt_package and path.name == 'latest.json':
            value = json.loads(path.read_text())
            value['version'] = '999.0.0'
            write_json(path, value)
        self.releases[0]['assets'].append({'name': path.name, 'size': path.stat().st_size,
            'digest': 'sha256:bad' if self.bad_digest else 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest(), 'state': 'uploaded'})


class PublisherTests(SignedArtifactFixture):
    def publisher(self, github):
        sys.path.insert(0, str(TOOLS))
        try:
            import release_windows
        finally:
            sys.path.pop(0)
        return release_windows.publish_public(self.root, self.config, 123, github)

    def setUp(self):
        super().setUp()
        self.manifest['build_provenance'] = {'repository': 'ZCchou/agent', 'workflow_run_id': '123', 'commit': 'a' * 40}
        write_json(self.root / 'release-manifest.json', self.manifest)
        self.prepare()

    def test_publication_advances_only_selected_feed_after_published_verified_assets(self):
        github = GithubFixture('a' * 40)
        self.publisher(github)
        self.assertEqual(github.feeds['updates/stable.json'], b'{"version":"0.14.0"}')
        self.assertEqual(github.feeds['updates/stable.json.sig'], b'older stable signature')
        self.assertEqual(github.feeds['updates/preview.json'], (self.root / 'public/latest.json').read_bytes())
        self.assertEqual(github.feeds['updates/preview.json.sig'], (self.root / 'public/latest.json.sig').read_bytes())
        self.assertEqual(github.releases[0]['target_commitish'], 'a' * 40)
        self.assertFalse(github.releases[0]['draft'])
        self.assertTrue(github.releases[0]['prerelease'])
        self.assertEqual({a['name'] for a in github.releases[0]['assets']}, {p.name for p in (self.root / 'public').iterdir()})

    def test_bad_remote_digest_and_publish_failure_never_advance_feed(self):
        for field in ('bad_digest', 'failed_publish', 'ref_failure'):
            with self.subTest(field=field):
                github = GithubFixture('a' * 40)
                setattr(github, field, True)
                with self.assertRaises(ValueError):
                    self.publisher(github)
                self.assertNotIn('updates/preview.json', github.feeds)
                self.assertEqual(github.feeds['updates/stable.json'], b'{"version":"0.14.0"}')

    def test_local_package_mutation_during_upload_never_publishes_or_advances_feed(self):
        github = GithubFixture('a' * 40)
        github.corrupt_package = True
        with self.assertRaises(ValueError):
            self.publisher(github)
        self.assertTrue(github.releases[0]['draft'])
        self.assertNotIn('updates/preview.json', github.feeds)

    def test_existing_git_tag_without_release_is_never_reused(self):
        github = GithubFixture('a' * 40)
        github.tag_exists = True
        with self.assertRaises(ValueError):
            self.publisher(github)
        self.assertEqual(github.releases, [])

    def test_existing_tag_is_immutable_and_wrong_provenance_is_rejected(self):
        github = GithubFixture('a' * 40)
        github.releases = [{'tag_name': 'v0.14.1-preview.1', 'draft': False, 'assets': []}]
        with self.assertRaises(ValueError):
            self.publisher(github)
        self.assertNotIn('updates/preview.json', github.feeds)
        for field, value in [('commit', 'b' * 40), ('run_success', False), ('source_private', True)]:
            github = GithubFixture('a' * 40)
            setattr(github, field, value)
            with self.assertRaises(ValueError):
                self.publisher(github)
            self.assertEqual(github.releases, [])

    def test_equal_or_newer_channel_version_cannot_be_replaced(self):
        for version in ('0.14.1', '0.14.2'):
            github = GithubFixture('a' * 40)
            github.feeds['updates/preview.json'] = json.dumps({'version': version}).encode()
            with self.assertRaises(ValueError):
                self.publisher(github)
            self.assertEqual(github.releases, [])

    def test_stable_release_uses_stable_feed_without_changing_preview(self):
        self.manifest.update(release_tag='v0.14.1', update_channel='stable')
        write_json(self.root / 'release-manifest.json', self.manifest)
        self.prepare()
        github = GithubFixture('a' * 40)
        github.feeds['updates/preview.json'] = b'older preview feed'
        github.feeds['updates/preview.json.sig'] = b'older preview signature'
        self.publisher(github)
        self.assertEqual(github.feeds['updates/preview.json'], b'older preview feed')
        self.assertEqual(github.feeds['updates/preview.json.sig'], b'older preview signature')
        self.assertEqual(github.feeds['updates/stable.json'], (self.root / 'public/latest.json').read_bytes())
        self.assertFalse(github.releases[0]['prerelease'])


if __name__ == '__main__':
    unittest.main()
