import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from platform_runner import Runner
from stage_progress import parse_progress


class ProgressTests(unittest.TestCase):
    def test_live_reader_handles_partial_lines_and_resume_rate(self):
        runner = Runner.__new__(Runner)
        runner.state = {'stage': 'training'}
        runner.stage_started = 100
        runner.last_progress_poll = 0
        runner.progress_offsets = {}
        runner.progress_fragments = {}
        runner.progress_values = {}
        runner.progress_baseline = None
        with tempfile.TemporaryDirectory() as folder, patch('platform_runner.emit') as emit:
            log = Path(folder) / 'training.log'
            log.write_text('step 8000/10000 splats 50', encoding='utf-8')
            with patch('platform_runner.time.monotonic', return_value=102):
                runner.report_log(log)
            self.assertNotIn('current', emit.call_args.kwargs)
            with log.open('a', encoding='utf-8') as stream:
                stream.write('000\r')
            with patch('platform_runner.time.monotonic', return_value=104):
                runner.report_log(log)
            self.assertEqual(emit.call_args.kwargs['gaussians'], 50000)
            self.assertNotIn('rate', emit.call_args.kwargs)
            with log.open('a', encoding='utf-8') as stream:
                stream.write('step 8100/10000 splats 51000\r')
            with patch('platform_runner.time.monotonic', return_value=106):
                runner.report_log(log)
            self.assertEqual(emit.call_args.kwargs['rate'], 50)
            self.assertEqual(emit.call_args.kwargs['remainingSeconds'], 38)
            self.assertEqual(emit.call_args.kwargs['elapsedSeconds'], 6)

    def test_lichtfeld_native_bar_with_ansi_and_carriage_returns(self):
        progress = parse_progress('training', '\x1b[32mTraining [█████   ] 50% '
                                  '[01m:18s<00m:42s] 1,000/2,000 | Loss: 0.1979 | Splats: 86,965\x1b[0m\r')
        self.assertEqual(progress['current'], 1000)
        self.assertEqual(progress['total'], 2000)
        self.assertEqual(progress['gaussians'], 86965)
        self.assertEqual(progress['loss'], 0.1979)
        self.assertEqual(progress['remainingSeconds'], 42)

    def test_spirula_native_training_metrics(self):
        progress = parse_progress('training', 'step 5501/6000 (91%) splats 100000 '
                                  '[elapsed 00:00:35.756 | ETA 00:00:02.958] rgb_loss=0.1366 ssim=0.5801 psnr=19.48')
        self.assertEqual(progress['current'], 5501)
        self.assertEqual(progress['gaussians'], 100000)
        self.assertEqual(progress['remainingSeconds'], 2.958)
        self.assertEqual(progress['psnr'], 19.48)

    def test_substeps_clear_old_counters(self):
        result = parse_progress('selection', 'Analyzed 90 candidates; scanned 100/200 frames...\n'
                                'Selecting from 90 candidates (maximum 20 images)...\n')
        self.assertEqual(result, {'substep': 'Choosing distinct, sharp views'})
        result = parse_progress('reconstruction', 'Processed file [5/10]\nGlobal bundle adjustment\n')
        self.assertEqual(result, {'substep': 'Refining cameras and scene geometry'})

    def test_densification_callback_is_a_structured_percentage(self):
        result = parse_progress('densification', '[ 45.25%] Matching chunk 2/5: processing pairs')
        self.assertEqual(result['current'], 45.25)
        self.assertEqual(result['unit'], 'percent')
        self.assertIn('chunk 2 of 5', result['substep'])

    def test_unknown_diagnostics_and_invalid_counters_are_not_progress(self):
        self.assertEqual(parse_progress('training', 'private path /tmp/input\nstep 100/50'), {})
        self.assertEqual(parse_progress('training', 'Training [==] 50% [00m:00s<00m:00s] Initializing...'), {})

    def test_final_native_count_replaces_training_count(self):
        result = parse_progress('training', 'Training [==] 100% [00m:18s<00m:00s] 100/200 | Splats: 86965\n'
                                'Training completed in 84.038s\nFinal splats: 34786')
        self.assertEqual(result, {'substep': 'Saving trained scene', 'gaussians': 34786})
