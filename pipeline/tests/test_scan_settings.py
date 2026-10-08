import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import scan_settings as settings
from pipeline_defaults import TRAINING, DENSIFICATION
from test_run_scan import fake_stage
from run_scan import main


class SettingsTests(unittest.TestCase):
    def test_defaults_preserve_training_and_densification(self):
        options = settings.load()
        self.assertEqual(settings.training(options, TRAINING['max_cap']), TRAINING)
        self.assertEqual(settings.densification(options), DENSIFICATION)

    def test_iteration_schedule_and_gaussian_override(self):
        options = settings.defaults()
        options['training'].update(iterations=1000, sparsify_steps=500, max_cap=9999)
        config = settings.training(options, 1234)
        self.assertEqual(config['save_steps'], [1000, 1500])
        self.assertEqual(config['grow_until_iter'], 1000)
        self.assertEqual(config['stop_refine'], 1100)
        self.assertEqual(config['max_cap'], 1234)
        options['training']['enable_sparsity'] = False
        self.assertEqual(settings.training(options, 1234)['save_steps'], [1000])

    def test_validation_and_actual_runner_forwarding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.mp4'
            source.write_bytes(b'video')
            config = root / 'settings.json'
            options = settings.defaults()
            options['selection'].update(sample_fps=2, start_seconds=1, end_seconds=5)
            options['training'].update(iterations=1000)
            config.write_text(json.dumps(options))
            args = [str(source), '--output', str(root / 'job'), '--settings', str(config), '--max-cap', '4321']
            with patch('pipeline_common.run_logged', side_effect=fake_stage) as run:
                self.assertEqual(main(args), 0)
                selection = run.call_args_list[0].args[0]
                self.assertEqual(selection[selection.index('--sample-fps') + 1], '2')
                reconstruction = run.call_args_list[1].args[0]
                resolved = Path(reconstruction[reconstruction.index('--settings') + 1])
                self.assertEqual(json.loads(resolved.read_text())['training']['max_cap'], 4321)
                self.assertEqual(main(args + ['--resume']), 0)
                self.assertEqual(run.call_count, 2)
            options['selection']['sample_fps'] = 3
            config.write_text(json.dumps(options))
            self.assertEqual(main(args + ['--resume']), 1)
            for invalid in [{'training': {'max_cap': 0}}, {'training': {'unknown': 1}},
                            {'selection': {'end_seconds': 1, 'start_seconds': 2}},
                            {'selection': {'preserve_coverage': True, 'max_images': 5}}]:
                config.write_text(json.dumps(invalid))
                with self.assertRaises(Exception):
                    settings.load(config)
