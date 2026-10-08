import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import image_notices
import source_bundle


class ReleaseEvidenceTests(unittest.TestCase):
    def test_collects_submodule_at_recorded_commit_and_preserves_mount(self):
        commit, child = 'a' * 40, 'b' * 40
        def response(url):
            if url.endswith('.gitmodules'):
                return io.BytesIO(b'[submodule "library"]\n path = external/library\n url = https://github.com/example/library.git\n')
            return io.BytesIO(b'archive bytes')
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(source_bundle, 'request', side_effect=response), \
                patch.object(source_bundle, 'api', side_effect=[
                    {'tree': [{'mode': '160000', 'path': 'external/library', 'sha': child}]},
                    {'tree': []}]):
            records = []
            source_bundle.collect('example/engine', commit, Path(folder), records, 'engine')
            self.assertEqual(records[1]['mount'], 'external/library')
            self.assertEqual(records[1]['commit'], child)
            self.assertEqual(records[1]['repository'], 'example/library')
            report = json.loads((Path(folder) / 'source-snapshots.json').read_text())
            self.assertFalse(report['correspondingSourceComplete'])

    def test_refuses_changed_cached_source(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(source_bundle, 'request', return_value=io.BytesIO(b'original')), \
                patch.object(source_bundle, 'api', return_value={'tree': []}):
            root, records = Path(folder), []
            source_bundle.collect('example/engine', 'a' * 40, root, records, 'engine')
            (root / records[0]['archive']).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'differs'):
                source_bundle.collect('example/engine', 'a' * 40, root, records, 'engine')

    def test_truncated_tree_cannot_complete_collection(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(source_bundle, 'request', return_value=io.BytesIO(b'archive')), \
                patch.object(source_bundle, 'api', return_value={'truncated': True, 'tree': []}):
            with self.assertRaisesRegex(ValueError, 'truncated'):
                source_bundle.collect('example/engine', 'a' * 40, Path(folder), [], 'engine')
            report = json.loads((Path(folder) / 'source-snapshots.json').read_text())
            self.assertFalse(report['snapshotsComplete'])

    def test_notice_bytes_and_digest_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            notice = root / 'COPYING.gz'
            notice.write_bytes(b'exact upstream compressed bytes')
            records, problems, output = {}, [], io.BytesIO()
            with tarfile.open(fileobj=output, mode='w') as archive:
                self.assertTrue(image_notices.add_document(archive, notice, root, records, problems))
            output.seek(0)
            with tarfile.open(fileobj=output) as archive:
                self.assertEqual(archive.extractfile(next(iter(records))).read(), notice.read_bytes())
            self.assertEqual(problems, [])
            self.assertTrue(image_notices.notice_name(notice))

    def test_notice_outside_allowed_root_is_a_finding(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            outside = root / 'LICENSE'
            outside.write_text('outside')
            records, problems = {}, []
            with tarfile.open(fileobj=io.BytesIO(), mode='w') as archive:
                self.assertFalse(image_notices.add_document(archive, outside, root / 'allowed', records, problems))
            self.assertEqual(records, {})
            self.assertEqual(len(problems), 1)


if __name__ == '__main__':
    unittest.main()
