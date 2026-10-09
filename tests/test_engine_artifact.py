import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import engine_release
from package_engine_artifact import ARCHIVE, CHECKSUMS, digest, package, verify


class EngineArtifactTests(unittest.TestCase):
    def create(self, directory):
        engine, output, source = directory / 'installed', directory / 'assets', directory / 'source'
        (engine / 'bin').mkdir(parents=True)
        for name in ('run_lichtfeld.sh', 'LichtFeld-Studio'):
            path = engine / 'bin' / name
            path.write_bytes(b'#!/bin/sh\nexit 0\n')
            path.chmod(0o755)
        source.mkdir()
        (source / 'CMakeLists.txt').write_text('project(LichtFeld)')
        (source / '.git').mkdir()
        (source / '.git/config').write_text('private build metadata')
        output.mkdir()
        (output / 'build-source-inventory.json').write_text('{}')
        (output / 'lichtfeld-build-dependencies.tar.gz.part000').write_bytes(b'matching build source')
        metadata = package(engine, output, 'a' * 64, source)
        return engine, output, metadata

    def test_archive_is_reproducible_with_installed_layout_modes_and_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine, output, metadata = self.create(Path(temporary))
            original = digest(output / ARCHIVE)
            os.utime(engine / 'bin/LichtFeld-Studio', (123456, 123456))
            package(engine, output, 'a' * 64)
            self.assertEqual(digest(output / ARCHIVE), original)
            verify(output)
            with tarfile.open(output / ARCHIVE) as archive:
                member = archive.getmember('lichtfeld/bin/run_lichtfeld.sh')
                self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))
                if os.name != 'nt':
                    self.assertTrue(member.mode & stat.S_IXUSR)
                self.assertEqual(archive.extractfile(member).read(), b'#!/bin/sh\nexit 0\n')
            with tarfile.open(output / 'lichtfeld-source.tar.gz.part000') as archive:
                self.assertIn('lichtfeld-source/CMakeLists.txt', archive.getnames())
                self.assertFalse(any('.git' in Path(name).parts for name in archive.getnames()))

    def test_corrupt_archive_manifest_and_missing_evidence_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, output, _ = self.create(Path(temporary))
            pinned = digest(output / CHECKSUMS)
            verify(output, pinned, [ARCHIVE])
            (output / ARCHIVE).write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'checksum differs'):
                verify(output, pinned, [ARCHIVE])
            (output / CHECKSUMS).write_text('changed manifest')
            with self.assertRaisesRegex(ValueError, 'manifest differs'):
                verify(output, pinned)

    def test_failed_lookup_only_builds_when_the_release_is_actually_absent(self):
        with patch.object(engine_release, 'gh', return_value=subprocess.CompletedProcess([], 1, '', 'release not found')):
            self.assertEqual(engine_release.resolve('tag', 'a' * 64, '.')['exists'], 'false')
        with patch.object(engine_release, 'gh', return_value=subprocess.CompletedProcess([], 1, '', 'HTTP 403: forbidden')):
            with self.assertRaisesRegex(RuntimeError, '403'):
                engine_release.resolve('tag', 'a' * 64, '.')

    def test_unfinished_draft_and_missing_source_cannot_be_used_or_published(self):
        with patch.object(engine_release, 'gh', return_value=subprocess.CompletedProcess([], 0, '{"isDraft":true}', '')):
            with self.assertRaisesRegex(RuntimeError, 'unfinished draft'):
                engine_release.resolve('tag', 'a' * 64, '.')
        with tempfile.TemporaryDirectory() as temporary, patch.object(engine_release, 'gh') as github:
            _, output, _ = self.create(Path(temporary))
            (output / 'lichtfeld-build-dependencies.tar.gz.part000').unlink()
            # Also remove the record to simulate a consistently checksummed but
            # incomplete artifact, rather than just corrupted downloaded bytes.
            p = output / CHECKSUMS
            p.write_text('\n'.join(line for line in p.read_text().splitlines()
                                   if 'lichtfeld-build-dependencies' not in line) + '\n')
            with self.assertRaisesRegex(ValueError, 'matching source'):
                engine_release.publish('tag', 'a' * 64, output, 'b' * 40, output)
            github.assert_not_called()

    def test_complete_published_identity_is_reused_but_incomplete_releases_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, output, metadata = self.create(Path(temporary))
            assets = [{'name': path.name} for path in output.iterdir()]
            def response(*args):
                body = json.dumps({'isDraft': False, 'assets': assets}) if args[1] == 'view' else ''
                return subprocess.CompletedProcess([], 0, body, '')
            with patch.object(engine_release, 'gh', side_effect=response):
                result = engine_release.resolve('tag', 'a' * 64, output)
                self.assertEqual(result['sha256'], metadata['sha256'])
                self.assertEqual(result['checksums_sha256'], digest(output / CHECKSUMS))
                assets.pop()
                with self.assertRaisesRegex(ValueError, 'missing'):
                    engine_release.resolve('tag', 'a' * 64, output)


if __name__ == '__main__':
    unittest.main()
