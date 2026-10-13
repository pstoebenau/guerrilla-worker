import contextlib
import io
import json
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import Mock, patch
import zipfile
import platform_runner as platform


def sog(path, count=3):
    with zipfile.ZipFile(path, 'w') as archive:
        metadata = {'count': count}
        for field in ('means', 'scales', 'quats', 'sh0'):
            metadata[field] = {'files': [field + '.webp']}
            archive.writestr(field + '.webp', b'verified texture')
        archive.writestr('meta.json', json.dumps(metadata))


class PreviewTests(unittest.TestCase):
    def runner(self, root, backend):
        runner = object.__new__(platform.Runner)
        runner.output = root
        runner.cap = 10
        runner.engine = platform.get_engine(backend)
        runner.state = {'stages': {'training': 'training-test'}}
        return runner

    def test_native_and_spirula_previews_are_immutable_sog_milestones(self):
        for backend in ('lichtfeld', 'spirula'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                runner = self.runner(root, backend)
                folder = root / 'training-test'
                folder.mkdir()
                def unpack(command, log, timeout_seconds):
                    self.assertGreater(timeout_seconds, 0)
                    self.assertLessEqual(timeout_seconds, 300)
                    self.assertEqual(command[1], 'convert')
                    self.assertIn('retained-checkpoints', str(command[2]))
                    self.assertEqual(Path(command[3]).suffix, '.ply')
                    Path(command[3]).write_bytes(b'frozen splats')
                def convert(source, candidate, log, cap, timeout_seconds):
                    self.assertGreater(timeout_seconds, 0)
                    self.assertLessEqual(timeout_seconds, 300)
                    self.assertEqual(Path(source).name, 'checkpoint.ply' if backend == 'lichtfeld' else 'splat.ply')
                    self.assertTrue(Path(source).is_file())
                    self.assertEqual(cap, 10)
                    sog(candidate)
                    candidate.with_suffix('.ppisp').write_bytes(b'sidecar')
                studio = Path('lichtfeld') if backend == 'lichtfeld' else None
                with patch.object(platform.common, 'run_logged', side_effect=unpack) as unpack_mock, \
                        patch.object(platform.common, 'studio_path', return_value=studio) as studio_mock, \
                        patch.object(platform.common, 'export_sog', side_effect=convert) as convert_mock:
                    for step in (100, 200):
                        snapshot = folder / 'retained-checkpoints' / str(step) / (f'step-{step}.ckpt' if backend == 'spirula' else f'checkpoint_{step}.resume')
                        snapshot.parent.mkdir(parents=True)
                        if backend == 'spirula':
                            snapshot.mkdir()
                            (snapshot / 'state.tar').write_bytes(b'recovery')
                            (snapshot / 'splat.ply').write_bytes(b'frozen splats')
                        else:
                            snapshot.write_bytes(b'frozen checkpoint')
                        runner.training_preview(snapshot, str(step).ljust(64,'a'))
                        runner.training_preview(snapshot, str(step).ljust(64,'a'))
                    self.assertEqual(convert_mock.call_count, 2)
                    self.assertEqual(unpack_mock.call_count, 2 if backend == 'lichtfeld' else 0)
                    self.assertEqual(studio_mock.call_count, 2 if backend == 'lichtfeld' else 0)
                previews = list((folder / 'previews').glob('*.sog'))
                self.assertEqual(len(previews), 2)
                self.assertTrue(all(platform.gaussian_count(file,10) == 3 for file in previews))
                self.assertTrue(all(file.with_suffix('.ppisp').read_bytes() == b'sidecar' for file in previews))
                self.assertFalse(list((folder / '.worker').iterdir()))

    def test_preview_timeout_handles_clock_rounding(self):
        # Adding the budget and subtracting the same reading rounds above 300.
        # Exercise both engine paths even when the clock has not advanced.
        with patch.object(platform.time, 'monotonic', return_value=1000.0000000000003):
            self.test_native_and_spirula_previews_are_immutable_sog_milestones()

    def test_failed_or_over_cap_conversion_never_publishes_a_preview(self):
        for count in (None, 11, 'timeout'):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                runner = self.runner(root, 'lichtfeld')
                snapshot = root / 'training-test/retained-checkpoints/a/checkpoint_100.resume'
                snapshot.parent.mkdir(parents=True)
                snapshot.write_bytes(b'recovery must survive')
                def convert(source, candidate, log, cap, timeout_seconds):
                    if count == 'timeout':
                        candidate.write_bytes(b'partial')
                        raise TimeoutError('preview budget exceeded')
                    if count is None:
                        candidate.write_bytes(b'partial')
                        raise RuntimeError('converter failed')
                    sog(candidate, count)
                with patch.object(platform.common, 'run_logged'), patch.object(platform.common, 'export_sog', side_effect=convert), patch.object(platform,'PROTOCOL_STREAM',io.StringIO()) as output:
                    runner.training_preview(snapshot, 'a'*64)
                    self.assertIn('SOG checkpoint preview could not be saved',output.getvalue())
                self.assertFalse(list((root / 'training-test/previews').glob('*.sog')))
                self.assertEqual(snapshot.read_bytes(),b'recovery must survive')

    def test_checkpoint_manifest_retains_all_sog_milestones_while_native_copies_roll(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(platform, 'runtime_versions', return_value={}), patch.object(platform, 'PROTOCOL_STREAM', io.StringIO()):
            root = Path(temporary).resolve()
            source = root / 'input.mp4'
            source.write_bytes(b'input')
            runner = platform.Runner(dict(scanId='scan', attemptId='attempt', backend='spirula', inputPath=str(source), outputPath=str(root / 'work'), settings={'training': {'iterations': 200}}, maxCap=3))
            runner.state.update(stage='training', stages={'training': 'training-test'})
            folder = runner.output / 'training-test'
            def convert(source, candidate, log, cap, timeout_seconds):
                sog(candidate)
            with patch.object(platform.common, 'export_sog', side_effect=convert), \
                    patch.object(platform.common, 'studio_path', side_effect=AssertionError('Spirula previews must not require LichtFeld')):
                for step in (100, 200):
                    native = folder / 'run' / f'step-{step:09d}.ckpt'
                    native.mkdir(parents=True)
                    (native.parent / 'config.json').write_text('{"cap_max":3}')
                    (native / 'splat.ply').write_bytes(b'frozen model')
                    with tarfile.open(native / 'state.tar', 'w') as archive:
                        for name, content in {'state.json': json.dumps({'full_resume': True, 'cur_num_splats': 3, 'step': step}).encode(), 'world.means.npy': b'positions'}.items():
                            member = tarfile.TarInfo(name)
                            member.size = len(content)
                            archive.addfile(member, io.BytesIO(content))
                    runner.poll_checkpoints(None)
            manifest = runner.state['stageCheckpoints']['training']['files']
            previews = [item['path'] for item in manifest if item['path'].endswith('.sog')]
            self.assertEqual(len(previews), 2)
            self.assertEqual(len(runner.state['checkpoints']), 1)
            self.assertTrue(all(not platform.excluded_path(path, runner.state['retention']['excludePaths']) for path in previews))

    def test_training_resumes_before_preview_and_archive_read_frozen_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(platform, 'runtime_versions', return_value={}), \
                patch.object(platform, 'PROTOCOL_STREAM', io.StringIO()):
            root = Path(temporary).resolve()
            source = root / 'input.mp4'
            source.write_bytes(b'input')
            runner = platform.Runner(dict(scanId='scan', attemptId='attempt', backend='lichtfeld',
                inputPath=str(source), outputPath=str(root / 'work'), settings={}, maxCap=3,
                archiveAck=True, backgroundArchive=True))
            runner.state.update(stage='training', stages={'training': 'training-test'})
            folder = runner.output / 'training-test'
            folder.mkdir()
            native = folder / 'checkpoint_30000.resume'
            native.write_bytes(b'closed checkpoint')
            paused = False
            phases = []

            @contextlib.contextmanager
            def pause(process):
                nonlocal paused
                paused = True
                try:
                    yield set()
                finally:
                    paused = False
                    native.write_bytes(b'engine overwrote native after resume')
                    phases.append('resumed')

            def preview(snapshot, identity):
                self.assertFalse(paused)
                self.assertEqual(snapshot.read_bytes(), b'closed checkpoint')
                phases.append('preview')

            def archive(stage):
                self.assertFalse(paused)
                snapshot = runner.path(runner.state['checkpoints'][0]['path'])
                self.assertEqual(snapshot.read_bytes(), b'closed checkpoint')
                self.assertEqual(runner.state['checkpoints'][0]['sha256'], platform.common.file_hash(snapshot))
                phases.append('archive')

            with patch.object(platform, 'paused_engine', side_effect=pause), \
                    patch.object(runner, 'training_preview', side_effect=preview), \
                    patch.object(runner, 'checkpoint', side_effect=archive):
                runner.poll_checkpoints(Mock(poll=Mock(return_value=None)))
            self.assertEqual(phases, ['resumed', 'preview', 'archive'])

if __name__ == '__main__':
    unittest.main()
