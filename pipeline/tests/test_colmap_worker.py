"""Reconstruction publication and resume boundaries, independent of a GPU."""
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import colmap_worker
import scan_settings


class ColmapWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.images = self.root / 'images'
        self.images.mkdir()
        for i in range(4):
            (self.images / f'{i}.png').write_bytes(bytes([i]))
        self.output = self.root / 'output'
        self.settings = scan_settings.defaults()['reconstruction']
        self.settings['refine_cameras'] = False
        self.pc = MagicMock(__version__='4.0.2', has_cuda=True)
        self.pc.extract_features.side_effect = lambda **kw: Path(kw['database_path']).write_bytes(b'features')
        self.model = MagicMock()
        self.model.num_reg_images.return_value = 3
        self.model.num_points3D.return_value = 40
        self.model.compute_mean_reprojection_error.return_value = .2
        self.model.images = {i: SimpleNamespace(name=f'{i}.png') for i in range(3)}
        self.pc.global_mapping.return_value = {0: self.model}
        self.pc.incremental_mapping.return_value = {0: self.model}
        def undistort(**kwargs):
            sparse = Path(kwargs['output_path']) / 'sparse'
            sparse.mkdir(parents=True)
            (sparse / 'cameras.bin').write_bytes(b'model')
        self.pc.undistort_images.side_effect = undistort
        patcher = patch.dict('sys.modules', pycolmap=self.pc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_reconstruction(self):
        return colmap_worker.reconstruct(self.images, self.output, self.settings)

    def test_failed_mapping_reuses_features_and_matches_without_publishing_dataset(self):
        self.pc.global_mapping.side_effect = [RuntimeError('GPU interrupted'), {0: self.model}]
        with self.assertRaisesRegex(RuntimeError, 'GPU interrupted'):
            self.run_reconstruction()
        self.assertFalse((self.output / 'colmap-result.json').exists())
        first_attempt = self.pc.global_mapping.call_args.kwargs['output_path']
        result = self.run_reconstruction()
        self.pc.extract_features.assert_called_once()
        self.pc.match_exhaustive.assert_called_once()
        self.assertNotEqual(first_attempt, self.pc.global_mapping.call_args.kwargs['output_path'])
        self.assertTrue(first_attempt.is_dir())
        self.assertEqual(result['unregistered_images'], ['3.png'])
        self.assertEqual((self.output / result['dataset_relative']).resolve(), Path(result['dataset_dir']).resolve())

    def test_changed_input_rejects_resume_before_gpu_work(self):
        self.run_reconstruction()
        (self.images / '0.png').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'inputs or settings changed'):
            self.run_reconstruction()
        self.pc.extract_features.assert_called_once()

    def test_incremental_mode_does_not_invoke_global_mapping_or_view_calibration(self):
        self.settings['mode'] = 'incremental'
        self.run_reconstruction()
        self.pc.incremental_mapping.assert_called_once()
        self.pc.global_mapping.assert_not_called()
        self.pc.calibrate_view_graph.assert_not_called()

    def test_insufficient_geometry_is_not_published(self):
        self.model.num_points3D.return_value = 0
        with self.assertRaisesRegex(RuntimeError, 'nonempty geometry'):
            self.run_reconstruction()
        self.assertFalse((self.output / 'colmap-result.json').exists())
        self.pc.undistort_images.assert_not_called()

    def test_cpu_build_fails_without_fallback(self):
        self.pc.has_cuda = False
        with self.assertRaisesRegex(RuntimeError, 'CUDA-enabled'):
            self.run_reconstruction()
        self.pc.extract_features.assert_not_called()

    def test_desktop_cleans_database_only_after_success(self):
        self.pc.global_mapping.side_effect = [RuntimeError('interrupted'), {0: self.model}]
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            colmap_worker.reconstruct(self.images, self.output, self.settings, retain_artifacts=False)
        database = self.pc.extract_features.call_args.kwargs['database_path']
        self.assertTrue(database.exists())
        result = colmap_worker.reconstruct(self.images, self.output, self.settings, retain_artifacts=False)
        self.assertFalse(database.exists())
        self.assertTrue(Path(result['dataset_dir']).is_dir())


if __name__ == '__main__':
    unittest.main()
