import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline_common import find_ply, fingerprint, selected_images, save_json


class CommonHelpersTest(unittest.TestCase):
    def test_atomic_write_retries_a_transient_reader_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'status.json'
            target.write_text('{"state":"old"}')
            original = Path.replace
            attempts = []

            def replace(source, destination):
                attempts.append(source)
                if len(attempts) == 1:
                    self.assertEqual(json.loads(target.read_text())['state'], 'old')
                    raise PermissionError('sharing violation')
                return original(source, destination)

            with patch.object(Path, 'replace', replace), patch('pipeline_common.time.sleep'):
                save_json(target, {'state': 'running'})
            self.assertEqual(json.loads(target.read_text())['state'], 'running')
            self.assertEqual(len(attempts), 2)
            self.assertEqual(list(target.parent.glob('*.tmp')), [])
            with patch.object(Path, 'replace', side_effect=PermissionError), patch('pipeline_common.time.sleep'):
                with self.assertRaises(PermissionError):
                    save_json(target, {'state': 'failed'})
            self.assertEqual(json.loads(target.read_text())['state'], 'running')
            self.assertEqual(list(target.parent.glob('*.tmp')), [])

    def test_selection_excludes_unselected_files_and_tracks_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            names = ["a.png", "b.png", "c.png"]
            for name in names + ["unselected.png"]:
                (folder / name).write_bytes(b"original")
            (folder / "selection.json").write_text(json.dumps({
                "selected": [{"filename": name} for name in names]}))
            paths = selected_images(folder)
            self.assertEqual([p.name for p in paths], names)
            before = fingerprint(paths)
            (folder / "b.png").write_bytes(b"modified")
            self.assertNotEqual(before, fingerprint(paths))

    def test_manifest_cannot_escape_selection_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "selection.json").write_text(json.dumps({
                "selected": [{"filename": "../outside.png"}]}))
            with self.assertRaises(ValueError):
                selected_images(folder)

    def test_final_ply_uses_numeric_iteration_not_lexical_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for name in ["splat_7000.ply", "splat_30000.ply", "points3D.ply"]:
                (folder / name).write_bytes(b"x" * 200)
            self.assertEqual(find_ply(folder).name, "splat_30000.ply")


if __name__ == "__main__":
    unittest.main()
