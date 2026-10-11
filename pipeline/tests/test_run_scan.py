import json
import hashlib
import gzip
import struct
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from run_scan import main
import run_scan


def fake_stage(command, log):
    command = [str(value) for value in command]
    if command[2].endswith('select_frames.py'):
        Path(command[4]).mkdir()
    else:
        output = Path(command[command.index('--output') + 1])
        output.mkdir(exist_ok=True)
        sog, spz = output/'result.sog', output/'result.spz'
        with zipfile.ZipFile(sog, 'w') as archive:
            metadata = {'count': 3}
            for field in ('means', 'scales', 'quats', 'sh0'):
                metadata[field] = {'files': [field + '.webp']}
                archive.writestr(field + '.webp', b'texture')
            archive.writestr('meta.json', json.dumps(metadata))
        spz.write_bytes(gzip.compress(struct.pack('<III4B', 0x5053474e, 2, 3, 0, 12, 0, 0) + bytes(3 * 19)))
        (output/'pipeline.json').write_text(json.dumps({
            'sog': str(sog), 'spz': str(spz), 'completed': ['colmap', 'densify', 'train', 'export']}))


class RunScanTest(unittest.TestCase):
    def test_invalid_spz_prevents_success(self):
        def invalid_stage(command, log):
            fake_stage(command, log)
            command = [str(value) for value in command]
            if command[2].endswith('reconstruct_splat.py'):
                output = Path(command[command.index('--output') + 1])
                (output / 'result.spz').write_bytes(b'invalid' * 100)
        with patch('pipeline_common.run_logged', side_effect=invalid_stage):
            self.assertEqual(main(self.args), 1)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root/'another scene.mp4'
        self.source.write_bytes(b'video')
        self.output = self.root/'job'
        self.args = [str(self.source), '--output', str(self.output), '--max-cap', '3000000']

    def test_cap_and_completed_resume(self):
        with patch('pipeline_common.run_logged', side_effect=fake_stage) as run:
            self.assertEqual(main(self.args), 0)
            command = run.call_args_list[1].args[0]
            self.assertEqual(command[command.index('--max-cap') + 1], 3000000)
            self.assertEqual(main(self.args + ['--resume']), 0)
            self.assertEqual(run.call_count, 2)
        self.source.write_bytes(b'changed')
        with patch('pipeline_common.run_logged') as run:
            self.assertEqual(main(self.args + ['--resume']), 1)
            run.assert_not_called()

    def test_url_download_and_completed_resume(self):
        args = ['https://photos.app.goo.gl/example', *self.args[1:]]
        def download(url, path):
            path.write_bytes(b'video')
        with patch('run_scan.download_video', side_effect=download) as fetch, \
             patch('pipeline_common.run_logged', side_effect=fake_stage):
            self.assertEqual(main(args), 0)
            self.assertEqual(main(args + ['--resume']), 0)
            self.assertEqual(fetch.call_count, 1)
            self.assertNotIn('supersplat', json.loads((self.output / 'scan.json').read_text()))

    def test_partial_selection_preserved(self):
        def fail(command, log):
            Path(command[4]).mkdir()
            raise RuntimeError('selection failed')
        with patch('pipeline_common.run_logged', side_effect=fail):
            self.assertEqual(main(self.args), 1)
        with patch('pipeline_common.run_logged', side_effect=fake_stage):
            self.assertEqual(main(self.args + ['--resume']), 0)
        self.assertTrue((self.output/'selected').is_dir())
        self.assertTrue((self.output/'selected-2').is_dir())

    def test_default_url_output_stays_at_working_directory_and_resumes(self):
        self.assertEqual(run_scan.SCANS_ROOT,
                         Path.cwd() / 'scans')
        url = 'https://photos.app.goo.gl/default-output'
        scans = self.root / 'scans'
        expected = scans / ('video-' + hashlib.sha256(url.encode()).hexdigest()[:12])
        with patch('run_scan.SCANS_ROOT', scans), \
             patch('run_scan.download_video', side_effect=lambda url, path: path.write_bytes(b'video')), \
             patch('pipeline_common.run_logged', side_effect=fake_stage) as run:
            self.assertEqual(main([url, '--max-cap', '3000000']), 0)
            self.assertTrue((expected / 'scan.json').is_file())
            self.assertEqual(main([url, '--max-cap', '3000000', '--resume']), 0)
            self.assertEqual(run.call_count, 2)

    def test_existing_output_and_changed_cap_rejected(self):
        with patch('pipeline_common.run_logged', side_effect=fake_stage):
            self.assertEqual(main(self.args), 0)
        with self.assertRaises(SystemExit):
            main(self.args)
        with patch('pipeline_common.run_logged') as run:
            self.assertEqual(main(self.args + ['--resume', '--max-cap', '2000000']), 1)
            run.assert_not_called()

    def test_removed_upload_flag_is_rejected_before_training(self):
        with patch('pipeline_common.run_logged') as run:
            with self.assertRaises(SystemExit):
                main(self.args + ['--upload'])
            run.assert_not_called()

    def test_reviewed_selection_and_colmap_resume(self):
        selection = self.root/'reviewed'
        selection.mkdir()
        for i in range(3):
            (selection/f'{i}.png').write_bytes(b'image')
        args = self.args + ['--selected-images', str(selection),
                            '--reconstruction-mode', 'incremental']
        with patch('pipeline_common.run_logged', side_effect=fake_stage) as run:
            self.assertEqual(main(args), 0)
            self.assertEqual(run.call_count, 1)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('--reconstruction-mode') + 1], 'incremental')
            self.assertEqual(Path(command[command.index('--images') + 1]).resolve(), selection.resolve())
            self.assertEqual(main(args + ['--resume']), 0)
            self.assertEqual(run.call_count, 1)
        with patch('pipeline_common.run_logged') as run:
            self.assertEqual(main(args + ['--resume', '--reconstruction-mode', 'global']), 1)
            (selection/'0.png').write_bytes(b'changed')
            self.assertEqual(main(args + ['--resume']), 1)
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
