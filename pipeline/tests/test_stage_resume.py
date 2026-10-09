import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import platform_runner as platform


class StageResumeTests(unittest.TestCase):
    def test_completed_outputs_survive_worker_upgrades_but_native_checkpoints_require_compatible_runtime(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(platform, 'runtime_versions', return_value={'engine':'new'}):
            root = Path(temporary)
            output = root / 'run'
            output.mkdir()
            identity = dict(backend='spirula', settings={}, maxCap=3, inputSha256='a'*64, runtimeVersions={'engine':'old'})
            selected = dict(directory='selection-a', files=[dict(path='selection-a/frame.jpg',size=1,sha256='b'*64)])
            state = dict(version=1,identity=identity,completed={'selection':selected},stages={'selection':'selection-a'})
            request = dict(scanId='scan',attemptId='attempt',backend='spirula',settings={},maxCap=3,
                           inputPath=str(root/'absent.mp4'),outputPath=str(output),inputSha256='a'*64,resume=True,selectiveResume=True)
            (output/'platform-state.json').write_text(json.dumps(state))
            runner = platform.Runner(request)
            self.assertEqual(runner.state['completed']['selection'],selected)
            self.assertEqual(runner.state['identity']['runtimeVersions'],{'engine':'new'})
            state['stageCheckpoints'] = {'training':dict(directory='training-a',files=[])}
            (output/'platform-state.json').write_text(json.dumps(state))
            with self.assertRaisesRegex(ValueError,'versions changed'):
                platform.Runner(request)

    def test_only_next_stage_dependencies_are_restored_on_an_empty_worker(self):
        for next_stage in ('densification', 'training', 'export', 'finished'):
            with self.subTest(stage=next_stage), tempfile.TemporaryDirectory() as temporary, \
                    patch.object(platform, 'runtime_versions', return_value={}), \
                    patch.object(platform, 'gaussian_count', return_value=3):
                root = Path(temporary)
                output = root / 'run'
                output.mkdir()
                remote = {
                    'selection-a/frame.jpg': b'frame',
                    'reconstruction-a/colmap-result.json': b'{"dataset_relative":"dataset"}',
                    'reconstruction-a/dataset/images/frame.jpg': b'image',
                    'reconstruction-a/dataset/sparse/cameras.bin': b'cameras',
                    'reconstruction-a/database.db': b'unneeded matching database',
                    'densification-a/result.json': b'{}',
                    'densification-a-dataset/images/frame.jpg': b'image',
                    'densification-a-dataset/sparse/points3D.ply': b'dense',
                    'training-a/final/splat_100.ply': b'model' * 30,
                    'export-a/result.sog': b'export',
                }
                import hashlib
                completed = {}
                stages = ('selection', 'reconstruction', 'densification', 'training', 'export')
                limit = {'densification': 2, 'training': 3, 'export': 4, 'finished': 5}[next_stage]
                for stage in stages[:limit]:
                    completed[stage] = dict(directory=stage + '-a', files=[
                        dict(path=name, size=len(data), sha256=hashlib.sha256(data).hexdigest())
                        for name, data in remote.items() if name.startswith(stage + '-a/') or name.startswith(stage + '-a-dataset/')])
                identity = dict(backend='lichtfeld', settings={'training': {'max_cap': 3}}, maxCap=3,
                                inputSha256='a' * 64, runtimeVersions={})
                state = dict(version=1, identity=identity, completed=completed,
                             stages={name: entry['directory'] for name, entry in completed.items()})
                (output / 'platform-state.json').write_text(json.dumps(state))
                request = dict(scanId='scan', attemptId='new', backend='lichtfeld', maxCap=3,
                               settings=identity['settings'], inputPath=str(root / 'absent-video.mp4'),
                               inputSha256='a' * 64, outputPath=str(output), resume=True, selectiveResume=True)
                restored = []

                def emit(kind, **event):
                    if kind != 'restore':
                        return
                    for name, data in remote.items():
                        if any(name == prefix or name.startswith(prefix + '/') for prefix in event['paths']):
                            file = output / name
                            if not file.exists():
                                file.parent.mkdir(parents=True, exist_ok=True)
                                file.write_bytes(data)
                                restored.append(name)
                    ack = output / '.worker/restored' / event['restoreId']
                    ack.parent.mkdir(parents=True, exist_ok=True)
                    ack.write_text('verified')

                with patch.object(platform, 'emit', side_effect=emit):
                    runner = platform.Runner(request)
                    def densify(_studio, dataset, folder, *_args, **_kwargs):
                        self.assertTrue((dataset / 'images/frame.jpg').exists())
                        folder.mkdir()
                        (folder / 'result.json').write_text('{}')
                    def train(folder, dataset, _options):
                        runner.restore(dataset / 'images', dataset / 'sparse')
                        (folder / 'final').mkdir(parents=True)
                        (folder / 'final/splat_100.ply').write_bytes(b'model' * 30)
                    runner.train_lichtfeld = train
                    def export(_studio, _ply, target, *_args, **_kwargs):
                        target.write_bytes(b'export')
                    with patch.object(platform.common, 'densify', side_effect=densify), \
                            patch.object(platform.common, 'export_sog', side_effect=export):
                        runner.run()
                expected = {
                    'densification': ['reconstruction-a/colmap-result.json', 'reconstruction-a/dataset/images/frame.jpg', 'reconstruction-a/dataset/sparse/cameras.bin'],
                    'training': ['densification-a-dataset/images/frame.jpg', 'densification-a-dataset/sparse/points3D.ply'],
                    'export': ['training-a/final/splat_100.ply'],
                    'finished': ['export-a/result.sog'],
                }[next_stage]
                self.assertEqual(sorted(restored), sorted(expected))
                self.assertFalse((root / 'absent-video.mp4').exists())
                for name, saved in completed.items():
                    self.assertEqual(runner.state['completed'][name], saved)

    def test_densification_checkpoint_contains_closed_chunks_but_not_partial_writes(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(platform, 'runtime_versions', return_value={}):
            root = Path(temporary)
            source = root / 'input.mp4'
            source.write_bytes(b'video')
            runner = platform.Runner(dict(scanId='scan', attemptId='attempt', backend='spirula',
                inputPath=str(source), outputPath=str(root / 'run'), maxCap=3, settings={}))
            folder = runner.output / 'densification-a'
            (folder / 'sparse/0').mkdir(parents=True)
            (folder / 'config.json').write_text('{}')
            (folder / 'sparse/0/cameras.bin').write_bytes(b'camera')
            closed, opened, broken = [folder / name for name in ('closed.npz', 'opened.npz', 'broken.npz')]
            for file in (closed, opened):
                with zipfile.ZipFile(file, 'w') as archive:
                    archive.writestr('metadata_json.npy', 'metadata')
                    archive.writestr('xyz.npy', 'xyz')
                    archive.writestr('rgb.npy', 'rgb')
            broken.write_bytes(b'incomplete zip')
            runner.state.update(stage='densification', stages={'densification':folder.name})
            runner.checkpoint = Mock()
            process = Mock()
            process.poll.return_value = None
            with patch.object(platform, 'paused_engine', return_value=contextlib.nullcontext({opened.resolve()})):
                runner.poll_checkpoints(process)
            files = runner.state['stageCheckpoints']['densification']['files']
            self.assertIn('densification-a/closed.npz', [item['path'] for item in files])
            self.assertNotIn('densification-a/opened.npz', [item['path'] for item in files])
            self.assertNotIn('densification-a/broken.npz', [item['path'] for item in files])
            runner.checkpoint.assert_called_once_with('densification')


if __name__ == '__main__':
    unittest.main()
