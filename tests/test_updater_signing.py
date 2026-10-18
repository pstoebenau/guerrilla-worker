import base64
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import configure_updater


class UpdaterSigningTests(unittest.TestCase):
    def test_public_release_requires_production_secret_and_rejects_test_override(self):
        for values in ({}, {'TAURI_SIGNING_PRIVATE_KEY': 'secret', 'GUERRILLA_UPDATER_TEST_PUBKEY': 'test'}):
            with self.subTest(values=list(values)), patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/main', **values}, clear=True):
                with self.assertRaises(ValueError):
                    configure_updater.configure()

    def test_production_configuration_never_writes_private_key_to_environment_files(self):
        with patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/main', 'TAURI_SIGNING_PRIVATE_KEY': 'secret'}, clear=True), \
                patch.object(configure_updater.subprocess, 'run') as generate:
            configure_updater.configure()
            generate.assert_not_called()

    def test_pull_request_signing_uses_an_ephemeral_key_outside_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = root / 'github-env'
            values = {key: value for key, value in os.environ.items() if not key.startswith(('TAURI_', 'GUERRILLA_UPDATER_', 'GITHUB_'))}
            values.update(GITHUB_REF='refs/pull/1/merge', GITHUB_ENV=str(environment), RUNNER_TEMP=str(root))
            with patch.dict(os.environ, values, clear=True):
                configure_updater.configure()
            exported = dict(line.split('=', 1) for line in environment.read_text().splitlines())
            key = Path(exported['TAURI_SIGNING_PRIVATE_KEY'])
            self.assertTrue(key.is_relative_to(root))
            self.assertFalse(key.is_relative_to(configure_updater.ROOT))
            self.assertNotIn(key.read_text().strip(), environment.read_text())
            self.assertIn('untrusted comment:', base64.b64decode(exported['GUERRILLA_UPDATER_TEST_PUBKEY']).decode())
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
