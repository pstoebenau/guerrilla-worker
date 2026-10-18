import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import publish_release
import reassemble_release
import release_assets
import release_version
import release_image


class ReleaseAutomationTests(unittest.TestCase):
    def collect(self, *args, **kwargs):
        # Inventory/feed tests isolate crypto, covered below with the real
        # upstream verifier and Tauri signer on the Linux package runner.
        with patch.object(publish_release, 'verify_signature', side_effect=lambda payload, signature, key, version: release_assets.read_signature(signature)):
            return publish_release.collect(*args, **kwargs)

    @staticmethod
    def fixture_signature(version='0.3.0'):
        return base64.b64encode(('untrusted comment: test fixture\n' + base64.b64encode(b'Ed' + b'x' * 72).decode() +
                                 f'\ntrusted comment: timestamp:1\tversion:{version}\n' + base64.b64encode(b'x' * 64).decode() + '\n').encode()).decode()

    def release_fixture(self, root):
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=publish_release.ROOT, text=True).strip()
        artifacts = root / 'artifacts'
        public = set()
        for platform, suffixes in {
            'Windows-X64': ('-setup.exe',),
            'Linux-X64': ('.AppImage', '.deb', '.rpm'),
            'macOS-ARM64': ('.dmg', '.app.tar.gz'),
        }.items():
            directory = artifacts / platform
            directory.mkdir(parents=True)
            packages = []
            updaters = []
            for suffix in suffixes:
                file = directory / (platform + suffix)
                file.write_bytes(('native package ' + file.name).encode())
                item = release_assets.record(file)
                packages.append({'file': item, 'assets': [item]})
                public.add(file.name)
                if suffix != '.dmg':
                    signature = file.with_name(file.name + '.sig')
                    signature.write_text(self.fixture_signature())
                    updaters.append({'name': file.name, 'signature': signature.name})
                    public.add(signature.name)
            (directory / (platform + '-notices.tar.gz')).write_bytes(b'bundled licenses and runtime evidence')
            if platform == 'Windows-X64':
                for suffix in ('-SHA256SUMS.txt', '-WebView2.json'):
                    file = directory / (platform + suffix)
                    file.write_bytes(b'Windows build evidence')
                    item = release_assets.record(file)
                    packages.append({'file': item, 'assets': [item]})
            report = {'updaters': updaters, 'platform': platform, 'version': '0.3.0', 'commit': commit, 'packages': packages,
                      'assets': [release_assets.record(file) for file in sorted(directory.iterdir())]}
            (directory / f'release-{platform}.json').write_text(json.dumps(report))
        return artifacts, commit, public

    def test_public_downloads_include_only_all_native_packages_source_and_one_checksum_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            artifacts, commit, packages = self.release_fixture(root)
            evidence = {file.relative_to(artifacts): release_assets.record(file)
                        for file in artifacts.rglob('*') if file.is_file()}
            destination = root / 'public'
            assets = self.collect(artifacts, destination, '0.3.0', commit)
            self.assertEqual({asset['name'] for asset in assets},
                             packages | {'guerrilla-worker-0.3.0-source.tar.gz', 'SHA256SUMS.txt', 'latest.json'})
            checksums = {name: digest for digest, name in
                         (line.split('  ', 1) for line in (destination / 'SHA256SUMS.txt').read_text().splitlines())}
            self.assertEqual(checksums, {asset['name']: asset['sha256'] for asset in assets if asset['name'] != 'SHA256SUMS.txt'})
            with tarfile.open(destination / 'guerrilla-worker-0.3.0-source.tar.gz') as source:
                self.assertTrue({'guerrilla-worker-0.3.0/LICENSE', 'guerrilla-worker-0.3.0/THIRD_PARTY_NOTICES'} <= set(source.getnames()))
            self.assertEqual(evidence, {file.relative_to(artifacts): release_assets.record(file)
                                       for file in artifacts.rglob('*') if file.is_file()})

    def test_unpublished_evidence_is_still_verified_and_missing_native_formats_are_refused(self):
        for failure in ('corrupt evidence', 'missing format'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                artifacts, commit, _ = self.release_fixture(root)
                if failure == 'corrupt evidence':
                    (artifacts / 'Windows-X64/Windows-X64-WebView2.json').write_bytes(b'changed')
                    message = 'checksum differs'
                else:
                    manifest = artifacts / 'Linux-X64/release-Linux-X64.json'
                    report = json.loads(manifest.read_text())
                    report['packages'] = [package for package in report['packages']
                                          if not package['file']['name'].endswith('.rpm')]
                    manifest.write_text(json.dumps(report))
                    message = 'exactly one Linux-X64 .rpm'
                with self.assertRaisesRegex(ValueError, message):
                    self.collect(artifacts, root / 'public', '0.3.0', commit)

    def test_feed_uses_verified_signatures_and_format_specific_immutable_urls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            artifacts, commit, _ = self.release_fixture(root)
            self.collect(artifacts, root / 'public', '0.3.0', commit, 'owner/repo')
            feed = json.loads((root / 'public/latest.json').read_text())
            self.assertEqual(feed['version'], '0.3.0')
            self.assertEqual(set(feed['platforms']), {'windows-x86_64', 'darwin-aarch64', 'linux-x86_64-appimage', 'linux-x86_64-deb', 'linux-x86_64-rpm'})
            for target, update in feed['platforms'].items():
                self.assertIn('https://github.com/owner/repo/releases/download/v0.3.0/', update['url'])
                self.assertEqual(update['signature'], self.fixture_signature())
                if target.startswith('linux-'):
                    self.assertTrue(update['url'].endswith({'appimage': '.AppImage', 'deb': '.deb', 'rpm': '.rpm'}[target.rsplit('-', 1)[1]]))

    def test_feed_refuses_missing_malformed_corrupt_or_misdirected_signatures(self):
        for failure in ('missing', 'malformed', 'corrupt', 'reference'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                artifacts, commit, _ = self.release_fixture(root)
                manifest = artifacts / 'Windows-X64/release-Windows-X64.json'
                report = json.loads(manifest.read_text())
                signature = manifest.parent / 'Windows-X64-setup.exe.sig'
                if failure == 'missing':
                    report['assets'] = [asset for asset in report['assets'] if asset['name'] != signature.name]
                    signature.unlink()
                elif failure == 'malformed':
                    signature.write_text('not a Tauri signature')
                    report['assets'] = [release_assets.record(manifest.parent / asset['name']) for asset in report['assets']]
                elif failure == 'corrupt':
                    signature.write_text('corruption after manifest')
                else:
                    report['updaters'][0]['name'] = '../outside.exe'
                manifest.write_text(json.dumps(report))
                with self.assertRaises(ValueError):
                    self.collect(artifacts, root / 'public', '0.3.0', commit)
                self.assertFalse((root / 'public/latest.json').exists())

    @unittest.skipUnless(shutil.which('minisign'), 'upstream Minisign is installed on the Linux release runner')
    def test_real_tauri_signature_refuses_tampering_wrong_key_and_wrong_signed_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'artifact.AppImage'
            data.write_bytes(b'real signed update artifact')
            keys = [root / 'first.key', root / 'second.key']
            environment = {key: value for key, value in os.environ.items() if not key.startswith('TAURI_SIGNING_')}
            for key in keys:
                result = subprocess.run(['bun', 'run', 'tauri', 'signer', 'generate', '--ci', '--password', '', '--write-keys', str(key)],
                                        cwd=release_assets.ROOT / 'desktop', capture_output=True, env=environment)
                self.assertEqual(result.returncode, 0, 'Tauri key generation failed')
            result = subprocess.run(['bun', 'run', 'tauri', 'signer', 'sign', '--private-key-path', str(keys[0]), '--password', '', '--app-version', '0.3.0', str(data)],
                                    cwd=release_assets.ROOT / 'desktop', capture_output=True, env=environment)
            self.assertEqual(result.returncode, 0, 'Tauri signing failed')
            public = keys[0].with_suffix('.key.pub').read_text().strip()
            signature = data.with_name(data.name + '.sig')
            publish_release.verify_signature(data, signature, public, '0.3.0')
            with self.assertRaisesRegex(ValueError, 'signed version differs'):
                publish_release.verify_signature(data, signature, public, '0.3.1')
            with self.assertRaisesRegex(ValueError, 'verification failed'):
                publish_release.verify_signature(data, signature, keys[1].with_suffix('.key.pub').read_text().strip(), '0.3.0')
            data.write_bytes(b'tampered artifact')
            with self.assertRaisesRegex(ValueError, 'verification failed'):
                publish_release.verify_signature(data, signature, public, '0.3.0')

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
            self.assertEqual(release_version.check_versions(root, '0.3.11'), '0.3.11')
            for name in files[:3]:
                self.assertEqual(json.loads((root / name).read_text())['version'], '0.3.11')
            for name in files[3:5]:
                self.assertEqual(tomllib.loads((root / name).read_text())['package']['version'], '0.3.11')
            for name, package in zip(files[5:], ('guerrilla-worker', 'guerrilla-worker-desktop')):
                records = tomllib.loads((root / name).read_text())['package']
                self.assertEqual(next(row['version'] for row in records if row['name'] == package), '0.3.11')
            desktop = root / 'desktop/src-tauri/Cargo.toml'
            desktop.write_text(desktop.read_text().replace('version = "0.3.11"', 'version = "0.3.0"', 1))
            with self.assertRaisesRegex(ValueError, 'Release versions differ'):
                release_version.check_versions(root, '0.3.11')

    def test_image_retries_reuse_existing_releases_and_refuse_conflicting_commits(self):
        release = {'tag_name': 'v0.3.5', 'target_commitish': 'a' * 40}
        self.assertFalse(release_version.existing_release('0.3.5', [], 'a' * 40))
        self.assertFalse(release_version.existing_release('0.3.6', [release], 'b' * 40))
        self.assertTrue(release_version.existing_release('0.3.5', [release], 'a' * 40))
        with self.assertRaisesRegex(ValueError, 'another commit'):
            release_version.existing_release('0.3.5', [release], 'b' * 40)

    def test_registry_reuse_requires_exact_identity_without_a_github_release(self):
        digest = 'sha256:' + 'a' * 64
        config = {'os': 'linux', 'architecture': 'amd64', 'config': {'Labels': {
            'org.opencontainers.image.version': '0.3.5',
            'org.opencontainers.image.revision': 'b' * 40,
            'org.opencontainers.image.source': 'https://github.com/owner/repo'}}}
        for response in (config, {'linux/amd64': config}):
            with patch.object(release_image, 'inspect', side_effect=[digest, json.dumps(response)]) as inspect:
                self.assertEqual(release_image.existing_image('owner/repo', '0.3.5', 'b' * 40), digest)
                self.assertEqual(inspect.call_args.args[0], 'ghcr.io/owner/repo@' + digest)
        for field in ('version', 'revision', 'source'):
            changed = json.loads(json.dumps(config))
            changed['config']['Labels']['org.opencontainers.image.' + field] = 'wrong'
            with patch.object(release_image, 'inspect', side_effect=[digest, json.dumps(changed)]):
                with self.assertRaisesRegex(ValueError, 'refusing reuse or overwrite'):
                    release_image.existing_image('owner/repo', '0.3.5', 'b' * 40)
        config['architecture'] = 'arm64'
        with patch.object(release_image, 'inspect', side_effect=[digest, json.dumps(config)]):
            with self.assertRaisesRegex(ValueError, 'platform'):
                release_image.existing_image('owner/repo', '0.3.5', 'b' * 40)
        with patch.object(release_image, 'inspect', return_value=None):
            self.assertIsNone(release_image.existing_image('owner/repo', '0.3.5', 'b' * 40))

    def test_version_allocation_skips_images_owned_by_other_commits(self):
        with patch.object(release_image, 'existing_image', side_effect=[release_image.ImageConflict(), None]) as inspect:
            self.assertEqual(release_image.select_version('owner/repo', '0.3.5', 'commit'), '0.3.6')
            self.assertEqual(inspect.call_args.args, ('owner/repo', '0.3.6', 'commit'))
        with patch.object(release_image, 'existing_image', return_value='sha256:existing'):
            self.assertEqual(release_image.select_version('owner/repo', '0.3.5', 'commit'), '0.3.5')
        with patch.object(release_image, 'existing_image', side_effect=RuntimeError('unauthorized')):
            with self.assertRaises(RuntimeError):
                release_image.select_version('owner/repo', '0.3.5', 'commit')

    def test_registry_errors_do_not_trigger_rebuilds(self):
        for error in ('unauthorized', 'timeout', 'too many requests', 'manifest unknown'):
            result = subprocess.CompletedProcess([], 1, '', error)
            with patch.object(release_image.subprocess, 'run', return_value=result):
                if error == 'manifest unknown':
                    self.assertIsNone(release_image.inspect('image:v1', 'template', optional=True))
                else:
                    with self.assertRaises(RuntimeError):
                        release_image.inspect('image:v1', 'template', optional=True)

    def test_canonical_names_preserve_formats_and_staged_checksums(self):
        cases = [('macOS-ARM64', 'macos-arm64', '.dmg'),
                 ('macOS-ARM64', 'macos-arm64', '.app.tar.gz'),
                 ('Windows-X64', 'windows-x86_64', '-setup.exe'),
                 *[('Linux-X64', 'ubuntu-x86_64', suffix) for suffix in ('.AppImage', '.deb', '.rpm')]]
        for platform, target, suffix in cases:
            with self.subTest(platform=platform, suffix=suffix), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / ('Old_aarch64' + suffix)
                source.write_bytes(b'package bytes')
                destination = root / 'assets'
                destination.mkdir()
                name = release_assets.package_name(source, platform, '0.3.5')
                self.assertEqual(name, f'guerrilla-worker-0.3.5-{target}{suffix}')
                package = release_assets.stage_file(source, destination, name=name)
                self.assertEqual(package['file'], release_assets.record(destination / name))
                self.assertEqual(package['assets'], [package['file']])

    def test_split_package_restores_exact_bytes_and_refuses_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'Guerrilla Worker_0.3.0_amd64.AppImage'
            payload = bytes(range(256)) * 4
            source.write_bytes(payload)
            destination = root / 'assets'
            destination.mkdir()
            # Archived v0.3.x split packages remain recoverable. New desktop
            # releases require one file and must reject oversized bundles.
            with self.assertRaisesRegex(ValueError, 'single-file'):
                release_assets.stage_file(source, destination, limit=100)
            package = {'file': {'name': 'Guerrilla.Worker_0.3.0_amd64.AppImage', 'size': len(payload),
                                'sha256': hashlib.sha256(payload).hexdigest()}, 'assets': []}
            for start in range(0, len(payload), 100):
                part = destination / f"{package['file']['name']}.part{len(package['assets']) + 1:03}"
                part.write_bytes(payload[start:start + 100])
                package['assets'].append(release_assets.record(part))
            (destination / (package['file']['name'] + '.parts.json')).write_text(json.dumps(package))
            self.assertTrue(all(item['size'] <= 100 for item in package['assets']))
            self.assertEqual(package['file']['name'], 'Guerrilla.Worker_0.3.0_amd64.AppImage')
            manifest = destination / (package['file']['name'] + '.parts.json')
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

    def test_small_packages_use_github_asset_names_before_checksumming(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'Guerrilla Worker_0.3.0_aarch64.dmg'
            source.write_bytes(b'actual image bytes')
            destination = root / 'assets'
            destination.mkdir()
            package = release_assets.stage_file(source, destination)
            target = destination / 'Guerrilla.Worker_0.3.0_aarch64.dmg'
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(package['file'], release_assets.record(target))
            self.assertEqual(package['assets'], [release_assets.record(target)])
            with self.assertRaisesRegex(ValueError, 'collision'):
                release_assets.stage_file(source, destination)

    def test_publication_refuses_missing_platforms_and_mixed_commits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            artifacts = root / 'artifacts'
            for platform in ('Windows-X64', 'Linux-X64'):
                directory = artifacts / platform
                directory.mkdir(parents=True)
                (directory / f'release-{platform}.json').write_text(json.dumps({
                    'platform': platform, 'version': '0.3.0', 'commit': 'a' * 40, 'assets': []}))
            with self.assertRaisesRegex(ValueError, 'Publication requires'):
                self.collect(artifacts, root / 'output', '0.3.0', 'a' * 40)
            directory = artifacts / 'macOS-ARM64'
            directory.mkdir()
            (directory / 'release-macOS-ARM64.json').write_text(json.dumps({
                'platform': 'macOS-ARM64', 'version': '0.3.0', 'commit': 'b' * 40, 'assets': []}))
            with self.assertRaisesRegex(ValueError, 'same version and commit'):
                self.collect(artifacts, root / 'output', '0.3.0', 'a' * 40)

    def test_publish_verification_rejects_missing_or_corrupt_uploaded_assets(self):
        expected = [{'name': 'package.dmg', 'size': 5, 'sha256': hashlib.sha256(b'bytes').hexdigest()}]
        remote = {'name': 'package.dmg', 'size': 5, 'state': 'uploaded',
                  'digest': 'sha256:' + expected[0]['sha256']}
        publish_release.verify_remote({'assets': [remote]}, expected)
        with self.assertRaisesRegex(ValueError, 'complete inventory'):
            publish_release.verify_remote({'assets': []}, expected)
        with self.assertRaisesRegex(ValueError, 'checksum differs'):
            publish_release.verify_remote({'assets': [{**remote, 'digest': 'sha256:wrong'}]}, expected)

    def test_new_and_resumed_drafts_are_verified_by_id_before_publication(self):
        asset = {'name': 'package.dmg', 'size': 5, 'sha256': hashlib.sha256(b'bytes').hexdigest()}
        release = {'id': 123, 'tag_name': 'v0.3.0', 'target_commitish': 'a' * 40, 'draft': True,
                   'assets': [{**asset, 'state': 'uploaded', 'digest': 'sha256:' + asset['sha256']}]}
        for existing, latest_tag in ((False, 'v0.2.0'), (True, 'v0.2.0'), (True, 'v0.3.10')):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                destination = root / 'assets'
                destination.mkdir()
                (destination / asset['name']).write_bytes(b'bytes')
                calls = []
                created = existing

                def transport(*args, **kwargs):
                    nonlocal created
                    calls.append(args)
                    if args[:2] == ('api', 'repos/owner/repo/releases'):
                        if '--method' in args:
                            request = json.loads(kwargs['input'])
                            self.assertEqual(request['target_commitish'], 'a' * 40)
                            self.assertEqual(request['tag_name'], 'v0.3.0')
                            self.assertTrue(request['draft'])
                            created = True
                            return json.dumps(release)
                        # Simulate a list endpoint that does not expose a newly
                        # created draft. Its create response must provide the ID.
                        self.assertFalse(created and not existing)
                        return json.dumps([[{'id': 99, 'tag_name': 'v0.2.0'}], [release] if created else []])
                    if args[:2] == ('api', 'repos/owner/repo/releases/latest'):
                        return json.dumps({'tag_name': latest_tag})
                    if args[:2] == ('api', 'repos/owner/repo/releases/123'):
                        return json.dumps(release)
                    if args[:2] in (('release', 'upload'), ('release', 'edit')):
                        return ''
                    raise AssertionError(f'Unexpected GitHub request (drafts cannot be found by tag): {args}')

                with patch.object(publish_release, 'gh', side_effect=transport):
                    publish_release.publish(destination, [asset], '0.3.0', 'a' * 40, 'owner/repo',
                                            'ghcr.io/owner/repo@sha256:' + 'b' * 64)
                notes = (root / 'release-notes.md').read_text()
                self.assertIn('docker pull ghcr.io/owner/repo:v0.3.0', notes)
                self.assertIn('ghcr.io/owner/repo@sha256:' + 'b' * 64, notes)
                self.assertEqual(sum('--method' in call for call in calls), int(not existing))
                verified = calls.index(('api', 'repos/owner/repo/releases/123'))
                published = next(i for i, call in enumerate(calls) if call[:2] == ('release', 'edit'))
                self.assertLess(verified, published)
                self.assertIn('--latest=false' if latest_tag == 'v0.3.10' else '--latest', calls[published])


if __name__ == '__main__':
    unittest.main()
