import argparse
import json
import os
from pathlib import Path
import tempfile
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import densification_worker as worker
from pipeline_defaults import DENSIFICATION


class DensificationWorkerTest(unittest.TestCase):
    def test_cache_is_configured_before_plugin_import_without_username(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ):
            root = Path(temporary).resolve()
            for key in ('USERNAME', 'USER', 'LOGNAME', 'LNAME', 'TORCHINDUCTOR_CACHE_DIR'):
                os.environ.pop(key, None)
            argv = ['worker', '--plugin', str(root / 'plugin'), '--dataset', str(root / 'dataset'),
                    '--output', str(root / 'attempt'), '--max-cap', '3']

            def import_plugin(_name):
                self.assertEqual(os.environ['TORCHINDUCTOR_CACHE_DIR'], str(root / '.cache/torchinductor'))
                self.assertFalse((root / 'attempt').exists())
                raise ImportError('stop after checking import environment')

            with patch('sys.argv', argv), patch.object(worker.importlib, 'import_module', side_effect=import_plugin):
                with self.assertRaisesRegex(ImportError, 'stop after checking'):
                    worker.main()

            os.environ['TORCHINDUCTOR_CACHE_DIR'] = str(root / 'custom-cache')
            with patch('sys.argv', argv), patch.object(worker.importlib, 'import_module', side_effect=ImportError):
                with self.assertRaises(ImportError):
                    worker.main()
            self.assertEqual(os.environ['TORCHINDUCTOR_CACHE_DIR'], str(root / 'custom-cache'))

    def test_cap_failure_preserves_model_and_successful_resume_publishes_once(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ):
            root = Path(temporary)
            dataset, output = root / 'dataset', root / 'attempt'
            sparse = dataset / 'sparse'
            sparse.mkdir(parents=True)
            (dataset / 'images').mkdir()
            for name in ('cameras.bin', 'images.bin', 'points3D.bin'):
                (sparse / name).write_bytes(b'original model')
            original_model = struct.pack('<Q', 3) + b'original model'
            (sparse / 'points3D.bin').write_bytes(original_model)
            original_cloud = b'ply\nformat binary_little_endian 1.0\ncomment original sparse cloud\nelement vertex 3\nend_header\n'
            (sparse / 'points3D.ply').write_bytes(original_cloud)
            parser = argparse.ArgumentParser()
            for key in ('scene_root', 'images_subdir', 'out_name', 'max_points', *DENSIFICATION):
                parser.add_argument('--' + key)
            parser.add_argument('--resume_chunks', action='store_true')
            count = [4]

            def densify(config, **kwargs):
                (output / 'sparse/0/points3D.ply').write_bytes(
                    f'ply\nformat binary_little_endian 1.0\nelement vertex {count[0]}\nend_header\n'.encode())
                return 0

            plugin = SimpleNamespace(build_argparser=lambda: parser, dense_init=densify,
                                     _cli_progress_callback=lambda: None)
            argv = ['worker', '--plugin', str(root / 'plugin'), '--dataset', str(dataset),
                    '--output', str(output), '--max-cap', '3']
            with patch('sys.argv', argv), patch.object(worker.importlib, 'import_module', return_value=plugin):
                with self.assertRaisesRegex(ValueError, 'outside'):
                    worker.main()
            self.assertEqual((sparse / 'points3D.ply').read_bytes(), original_cloud)
            self.assertEqual((sparse / 'points3D.bin').read_bytes(), original_model)
            count[0] = 3
            with patch('sys.argv', argv + ['--resume']), \
                    patch.object(worker.importlib, 'import_module', return_value=plugin):
                worker.main()
            report = json.loads((output / 'result.json').read_text())
            self.assertEqual(report['points'], 3)
            self.assertEqual(report['settings']['roma_setting'], 'high')
            self.assertEqual((sparse / 'points3D-sparse.ply').read_bytes(), original_cloud)
            (sparse / 'points3D.ply').unlink()  # Recover a crash before publishing.
            with patch('sys.argv', argv + ['--resume']), \
                    patch.object(worker.importlib, 'import_module', return_value=plugin), \
                    patch.object(plugin, 'dense_init') as run:
                worker.main()
                run.assert_not_called()
            self.assertTrue((sparse / 'points3D.ply').exists())


if __name__ == '__main__':
    unittest.main()
