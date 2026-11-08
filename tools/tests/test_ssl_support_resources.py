"""Build-interpreter TLS data admission without changing certificate validation."""
import hashlib
import importlib.util
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]


class SslSupportResourceTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('rag_runtime_assets_ssl', TOOLS / 'rag_runtime_assets.py')
        self.assets = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.assets)
        self.temporary = tempfile.TemporaryDirectory(prefix='agent-ssl-assets-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ssl_module = self.root / 'ssl.py'
        self.ssl_module.write_text('# ordinary CPython stdlib\n')
        self.resource = self.root / 'codex-rejected-cas.pem'

    def test_standard_stdlib_without_optional_resource_has_no_extra_data(self):
        with patch.object(ssl, '__file__', str(self.ssl_module)):
            self.assertEqual(self.assets.ssl_support_resources(), [])

    def test_referenced_resource_is_mandatory(self):
        self.ssl_module.write_text("load_verify_locations(cafile='codex-rejected-cas.pem')\n")
        with patch.object(ssl, '__file__', str(self.ssl_module)), self.assertRaisesRegex(ValueError, 'required'):
            self.assets.ssl_support_resources()

    def test_referenced_resource_hash_mismatch_fails_closed(self):
        self.ssl_module.write_text("load_verify_locations(cafile='codex-rejected-cas.pem')\n")
        self.resource.write_bytes(b'altered certificate data')
        with patch.object(ssl, '__file__', str(self.ssl_module)), self.assertRaisesRegex(ValueError, 'SHA256'):
            self.assets.ssl_support_resources()

    def test_verified_resource_is_copied_to_frozen_ssl_module_directory(self):
        self.ssl_module.write_text("load_verify_locations(cafile='codex-rejected-cas.pem')\n")
        self.resource.write_bytes(b'synthetic public certificate fixture')
        digest = hashlib.sha256(self.resource.read_bytes()).hexdigest()
        bundle = self.root / 'rag-runtime'
        (bundle / '_internal').mkdir(parents=True)
        with patch.object(ssl, '__file__', str(self.ssl_module)), \
                patch.object(self.assets, 'SSL_SUPPORT_RESOURCE_SHA256', digest):
            self.assertEqual(self.assets.ssl_support_resources(), [self.resource])
            records = self.assets.stage_ssl_support_resources(bundle)
        target = bundle / '_internal/codex-rejected-cas.pem'
        self.assertEqual(target.read_bytes(), self.resource.read_bytes())
        self.assertEqual(records[0]['sha256'], digest)
        self.assertEqual(records[0]['path'], '_internal/codex-rejected-cas.pem')

    def test_production_pin_matches_the_diagnosed_resource(self):
        self.assertEqual(self.assets.SSL_SUPPORT_RESOURCE_SHA256,
                         'c916ce1b14ed8ca69e9b5c9fab22799a900dd610d6d4ec9b1347f4fd48e53718')

    def test_core_freeze_recipe_is_included_in_release_source_identity(self):
        from tools.offline_delivery import source_identity
        self.assertIn('services/agent-core/agent-core.spec', source_identity())
