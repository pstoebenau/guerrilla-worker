import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import publish_release
import reassemble_release
import release_assets
import release_version


class ReleaseAutomationTests(unittest.TestCase):
    def test_one_numeric_version_is_allocated_and_reruns_reuse_it(self):
        releases = [{'tag_name': 'v0.3.9', 'prerelease': False, 'target_commitish': 'old'},
                    {'tag_name': 'v0.3.10', 'prerelease': False, 'target_commitish': 'old'},
                    {'tag_name': 'linux-v9.0.0', 'prerelease': False, 'target_commitish': 'old'},
                    {'tag_name': 'v9.0.0', 'prerelease': True, 'target_commitish': 'old'}]
        self.assertEqual(release_version.next_version('0.3.0', releases, 'new'), '0.3.11')
        self.assertEqual(release_version.next_version('0.4.0', releases, 'new'), '0.4.0')
        self.assertEqual(release_version.next_version('0.3.0', releases, 'old'), '0.3.10')

    def test_versions_are_applied_to_both_apps_and_locked_rust_packages(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = ('package.json', 'desktop/package.json', 'desktop/src-tauri/tauri.conf.json',
                     'rust/worker/Cargo.toml', 'desktop/src-tauri/Cargo.toml',
                     'Cargo.lock', 'desktop/src-tauri/Cargo.lock')
            for name in files:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(release_version.ROOT / name, target)
            release_version.apply_version(root, '0.3.11')
            for name in files[:3]:
                self.assertEqual(json.loads((root / name).read_text())['version'], '0.3.11')
            for name in files[3:5]:
                self.assertEqual(tomllib.loads((root / name).read_text())['package']['version'], '0.3.11')
            for name, package in zip(files[5:], ('guerrilla-worker', 'guerrilla-worker-desktop')):
                records = tomllib.loads((root / name).read_text())['package']
                self.assertEqual(next(row['version'] for row in records if row['name'] == package), '0.3.11')

    def test_split_package_restores_exact_bytes_and_refuses_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'Guerrilla-Worker-0.3.0-amd64.AppImage'
            payload = bytes(range(256)) * 4
            source.write_bytes(payload)
            destination = root / 'assets'
            destination.mkdir()
            package = release_assets.stage_file(source, destination, limit=100)
            self.assertTrue(all(item['size'] <= 100 for item in package['assets']))
            manifest = destination / (source.name + '.parts.json')
            restored = reassemble_release.reassemble(manifest)
            self.assertEqual(restored.read_bytes(), payload)
            if os.name != 'nt':
                self.assertTrue(restored.stat().st_mode & 0o111)
            with self.assertRaisesRegex(ValueError, 'overwrite'):
                reassemble_release.reassemble(manifest)
            restored.unlink()
            (destination / package['assets'][1]['name']).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'Invalid part'):
                reassemble_release.reassemble(manifest)
            self.assertFalse(restored.exists())
            self.assertFalse(list(destination.glob('*.assembling')))

    def test_publication_refuses_missing_platforms_and_mixed_commits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            artifacts = root / 'artifacts'
            for platform in ('Windows-X64', 'Linux-X64', 'macOS-ARM64'):
                directory = artifacts / platform
                directory.mkdir(parents=True)
                (directory / f'release-{platform}.json').write_text(json.dumps({
                    'platform': platform, 'version': '0.3.0', 'commit': 'a' * 40, 'assets': []}))
            with self.assertRaisesRegex(ValueError, 'Publication requires'):
                publish_release.collect(artifacts, root / 'output', '0.3.0', 'a' * 40)
            directory = artifacts / 'macOS-X64'
            directory.mkdir()
            (directory / 'release-macOS-X64.json').write_text(json.dumps({
                'platform': 'macOS-X64', 'version': '0.3.0', 'commit': 'b' * 40, 'assets': []}))
            with self.assertRaisesRegex(ValueError, 'same version and commit'):
                publish_release.collect(artifacts, root / 'output', '0.3.0', 'a' * 40)

    def test_publish_verification_rejects_missing_or_corrupt_uploaded_assets(self):
        expected = [{'name': 'package.dmg', 'size': 5, 'sha256': hashlib.sha256(b'bytes').hexdigest()}]
        remote = {'name': 'package.dmg', 'size': 5, 'state': 'uploaded',
                  'digest': 'sha256:' + expected[0]['sha256']}
        publish_release.verify_remote({'assets': [remote]}, expected)
        with self.assertRaisesRegex(ValueError, 'complete inventory'):
            publish_release.verify_remote({'assets': []}, expected)
        with self.assertRaisesRegex(ValueError, 'checksum differs'):
            publish_release.verify_remote({'assets': [{**remote, 'digest': 'sha256:wrong'}]}, expected)


if __name__ == '__main__':
    unittest.main()
