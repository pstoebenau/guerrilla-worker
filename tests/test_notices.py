import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from package_notices import package


class NoticePackagingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / 'source'
        self.target = Path(self.directory.name) / 'target'
        for name in ('THIRD_PARTY_NOTICES', 'README.md', 'SECURITY.md', 'docs/dependency-review.md',
                     'docs/release-process.md', 'docs/release-notes-0.1.0.md', 'docs/model-terms.md',
                     'docs/linux-release-review.md',
                     'packages/protocol/LICENSE', 'legal/COLMAP-LICENSE.txt',
                     'legal/RoMaV2-LICENSE.txt', 'legal/DINOv3-LICENSE.txt',
                     'legal/LichtFeld-LICENSE.txt', 'legal/Spirula-LICENSE.txt',
                     'legal/Densification-LICENSE.txt', 'legal/Node-LICENSE.txt'):
            file = self.source / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('reviewed document')

    def test_candidate_stages_pending_marker_without_selecting_license(self):
        package(self.source, self.target)
        self.assertTrue((self.target / 'LICENSE-PENDING').is_file())
        self.assertFalse((self.source / 'LICENSE').exists())
        self.assertTrue((self.target / 'THIRD_PARTY_NOTICES').is_file())

    def test_release_rejects_missing_owner_license_and_missing_manifest(self):
        with self.assertRaisesRegex(ValueError, 'owner-accepted'):
            package(self.source, self.target, True)
        (self.source / 'LICENSE').write_text('accepted license')
        with self.assertRaisesRegex(ValueError, 'manifest'):
            package(self.source, self.target, True)

    def test_release_copies_only_checksum_verified_reviewed_notices(self):
        (self.source / 'LICENSE').write_text('accepted license')
        legal = self.source / 'legal'
        legal.mkdir(exist_ok=True)
        content = b'upstream copyright and license'
        (legal / 'component-LICENSE.txt').write_bytes(content)
        (legal / 'unreviewed.txt').write_text('not included')
        manifest = {'version': 1, 'redistributionReviewed': True,
                    'files': [{'name': 'component-LICENSE.txt', 'sha256': hashlib.sha256(content).hexdigest()}]}
        (legal / 'manifest.json').write_text(json.dumps(manifest))
        package(self.source, self.target, True)
        self.assertEqual((self.target / 'legal/component-LICENSE.txt').read_bytes(), content)
        self.assertFalse((self.target / 'legal/unreviewed.txt').exists())
        (legal / 'component-LICENSE.txt').write_text('changed after review')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            package(self.source, self.target, True)

    def test_manifest_rejects_path_escape(self):
        legal = self.source / 'legal'
        legal.mkdir(exist_ok=True)
        (legal / 'manifest.json').write_text(json.dumps({'version': 1, 'files': [{'name': '../outside.txt', 'sha256': '0' * 64}]}))
        with self.assertRaisesRegex(ValueError, 'filename'):
            package(self.source, self.target)

    def test_version_two_checks_integrity_without_blanket_attestation(self):
        (self.source / 'LICENSE').write_text('accepted license')
        document = self.source / 'legal/component-LICENSE.txt'
        document.write_bytes(b'upstream notice')
        manifest = {'version': 2, 'files': [{'name': document.name,
                    'sha256': hashlib.sha256(document.read_bytes()).hexdigest()}]}
        (self.source / 'legal/manifest.json').write_text(json.dumps(manifest))
        package(self.source, self.target, True)
        document.write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            package(self.source, self.target, True)
