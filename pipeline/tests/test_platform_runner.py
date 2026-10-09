import contextlib
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

import platform_runner as platform


def spz(path, count=3):
    with gzip.open(path, 'wb') as stream:
        stream.write(struct.pack('<III4B', 0x5053474e, 3, count, 0, 12, 0, 0))
        stream.write(bytes(count * 20))


class PlatformTests(unittest.TestCase):
    def test_background_stage_completes_before_local_snapshot_ack(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(platform, 'runtime_versions', return_value={}):
            request = self.request(Path(temporary))
            request.update(archiveAck=True, backgroundArchive=True, archiveTimeoutSeconds=0)
            runner = platform.Runner(request)
            events = []
            def event(kind, **data):
                events.append((kind, data))
                if kind == 'checkpoint':
                    directory = runner.output / '.archive-acks'
                    directory.mkdir(exist_ok=True)
                    (directory / (data['checkpointId'] + '.ready')).write_text('ready')
            def select(folder):
                folder.mkdir()
                (folder / 'image.jpg').write_bytes(b'image')
            with patch.object(platform, 'emit', side_effect=event):
                runner.stage('selection', select)
            self.assertEqual(events[-2][1]['status'], 'completed')
            self.assertEqual(events[-1][0], 'checkpoint')
            self.assertFalse((runner.output / '.archive-acks' / events[-1][1]['checkpointId']).exists())

    def test_saved_export_skips_training_and_preserves_durable_training_outputs(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(platform, 'runtime_versions', return_value={}), \
                patch.object(platform, 'gaussian_count', return_value=3), \
                patch.object(platform.common, 'studio_path', return_value='studio'):
            request = self.request(Path(temporary))
            runner = platform.Runner(request)
            def train(folder):
                (folder / 'final').mkdir(parents=True)
                (folder / 'final/splat.ply').write_bytes(b'large model')
                (folder / 'training.log').write_text('trained')
            runner.spirula = lambda: runner.stage('training', train) / 'final/splat.ply'
            def export(_ply, target, *_args, **_kwargs):
                target.write_bytes(b'compressed model')
            with patch.object(platform.common, 'export_sog', side_effect=export), \
                    patch.object(platform.common, 'export_spz') as spz_export:
                runner.run()
                spz_export.assert_not_called()
            self.assertNotIn('spz', runner.state['result'])
            self.assertTrue(any(item['path'].endswith('.ply') for item in runner.state['completed']['training']['files']))
            request['resume'] = True
            resumed = platform.Runner(request)
            resumed.spirula = Mock(side_effect=AssertionError('Must not repeat training'))
            resumed.run()
            resumed.spirula.assert_not_called()

    def test_spirula_snapshots_survive_native_checkpoint_overwrites(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(platform, 'runtime_versions', return_value={'engine': 'pinned'}), \
                patch.object(platform, 'PROTOCOL_STREAM', io.StringIO()):
            request = self.request(Path(temporary))
            runner = platform.Runner(request)
            runner.state.update(stage='training', stages={'training': 'training-attempt'})
            native = runner.output / 'training-attempt/run/step-000008000.ckpt'
            native.mkdir(parents=True)

            def write_native(generation):
                with tarfile.open(native / 'state.tar', 'w') as archive:
                    contents = {
                        'state.json': json.dumps({'full_resume': True, 'cur_num_splats': 3,
                                                  'generation': generation}).encode(),
                        'world.means.npy': generation.encode(),
                        'world.opacities.npy': generation.encode(),
                    }
                    for name, content in contents.items():
                        member = tarfile.TarInfo(name)
                        member.size = len(content)
                        archive.addfile(member, io.BytesIO(content))
                (native / 'splat.ply').write_bytes(generation.encode())
                (native / 'sidecars').mkdir(exist_ok=True)
                (native / 'sidecars/cameras.json').write_text(generation)
                (native.parent / 'config.json').write_bytes(json.dumps({'generation': generation, 'cap_max': 3}).encode())

            def poll(open_paths=()):
                runner.last_checkpoint_poll = -100
                with patch.object(platform, 'paused_engine',
                                  return_value=contextlib.nullcontext(set(open_paths))):
                    runner.poll_checkpoints(Mock(poll=Mock(return_value=None)))

            write_native('first')
            config = native.parent / 'config.json'
            original_config = config.read_bytes()
            config.unlink()
            poll()
            self.assertEqual(runner.state.get('checkpoints', []), [])
            config.write_bytes(original_config)
            poll([config.resolve()])
            self.assertEqual(runner.state.get('checkpoints', []), [])
            # A closed state.tar does not make an open sibling safe to snapshot.
            poll([(native / 'sidecars/cameras.json').resolve()])
            self.assertEqual(runner.state.get('checkpoints', []), [])
            poll()
            first = runner.path(runner.state['checkpoints'][0]['path'])
            self.assertIn('retained-checkpoints', first.parts)
            self.assertNotEqual(first, native)
            original_tar = (first / 'state.tar').read_bytes()
            write_native('second generation')
            poll()
            self.assertEqual(len(runner.state['checkpoints']), 1)
            second = runner.path(runner.state['checkpoints'][0]['path'])
            self.assertNotEqual(first, second)
            self.assertEqual((first / 'state.tar').read_bytes(), original_tar)
            self.assertEqual((first.parent / 'config.json').read_bytes(), original_config)
            self.assertEqual((second.parent / 'config.json').read_bytes(), config.read_bytes())
            self.assertEqual(runner.state['checkpoints'][0]['configSha256'], platform.common.file_hash(second.parent / 'config.json'))
            self.assertEqual((first / 'splat.ply').read_bytes(), b'first')
            self.assertEqual((first / 'sidecars/cameras.json').read_text(), 'first')
            self.assertEqual((second / 'sidecars/cameras.json').read_text(), 'second generation')
            poll()
            self.assertEqual(len(runner.state['checkpoints']), 1)
            # A further native overwrite must not invalidate either saved hash.
            write_native('uncommitted third generation')
            request['resume'] = True
            restored = platform.Runner(request)
            self.assertEqual(len(restored.state['checkpoints']), 1)
            self.assertTrue(platform.excluded_path(first.relative_to(runner.output).as_posix(), runner.state['retention']['excludePaths']))
            # Superseded bytes remain local until an actual archive acknowledgement.
            self.assertTrue(first.exists())
            (second.parent / 'config.json').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'checkpoint configuration changed'):
                platform.Runner(request)

    def test_rolling_checkpoint_prunes_only_after_archive_ack_and_keeps_open_files(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(platform, 'runtime_versions', return_value={}), \
                patch.object(platform, 'PROTOCOL_STREAM', io.StringIO()):
            request = self.request(Path(temporary))
            request['backend'] = 'lichtfeld'
            request['settings'] = {}
            request['archiveAck'] = True
            runner = platform.Runner(request)
            runner.state.update(stage='training', stages={'training': 'training-test'})
            folder = runner.output / 'training-test'
            folder.mkdir()
            old = folder / 'checkpoint_100.resume'
            old.write_bytes(b'old optimizer')
            runner.checkpoint = Mock()
            with patch.object(platform, 'paused_engine', return_value=contextlib.nullcontext(set())):
                runner.poll_checkpoints(Mock(poll=Mock(return_value=None)))
            old_snapshot = runner.path(runner.state['checkpoints'][0]['path'])
            latest = folder / 'checkpoint_200.resume'
            latest.write_bytes(b'latest optimizer')
            def failed_archive(stage):
                self.assertTrue(old.exists())
                self.assertTrue(old_snapshot.exists())
                self.assertEqual(len(runner.state['checkpoints']), 1)
                raise TimeoutError('archive failed')
            runner.checkpoint = failed_archive
            runner.last_checkpoint_poll = -100
            with patch.object(platform, 'paused_engine', return_value=contextlib.nullcontext(set())):
                with self.assertRaisesRegex(TimeoutError, 'archive failed'):
                    runner.poll_checkpoints(Mock(poll=Mock(return_value=None)))
            self.assertTrue(old_snapshot.exists())
            plan = platform.training_retention_plan(runner.output, runner.state)
            runner.prune_training(plan, {old.resolve()}, latest)
            self.assertTrue(old.exists())
            self.assertFalse(old_snapshot.exists())
            self.assertTrue(latest.exists())
            runner.prune_training(plan, active_checkpoint=latest)
            self.assertFalse(old.exists())
            self.assertTrue(runner.path(runner.state['checkpoints'][0]['path']).exists())

    def test_compact_clone_preserves_final_ply_and_rewrites_completed_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            training = root / 'training-test'
            training.mkdir()
            (training / 'splat_100.ply').write_bytes(b'older' * 30)
            (training / 'splat_200.ply').write_bytes(b'final' * 30)
            (training / 'config.json').write_bytes(b'{"iterations":200}')
            snapshots = []
            for step in (100, 200):
                path = (root if step == 100 else training) / 'retained-checkpoints' / str(step) / f'checkpoint_{step}.resume'
                path.parent.mkdir(parents=True)
                path.write_bytes(str(step).encode())
                snapshots.append(dict(path=path.relative_to(root).as_posix(), sha256=platform.common.file_hash(path)))
            state = dict(identity={'backend': 'lichtfeld'}, stages={'training': training.name}, checkpoints=snapshots,
                         completed={'training': dict(directory=training.name, files=platform.artifact_manifest(root))})
            platform.common.save_json(root / 'platform-state.json', state)
            original_state = (root / 'platform-state.json').read_bytes()
            with self.assertRaisesRegex(ValueError, 'must be relative'):
                platform.retained_path(root, training)
            with self.assertRaisesRegex(ValueError, 'must be relative'):
                platform.retained_path(root, '../escape')
            plan = platform.training_retention_plan(root, state)
            self.assertEqual((root / 'platform-state.json').read_bytes(), original_state)
            self.assertEqual(plan['latestCheckpoint'], snapshots[-1])
            with tempfile.TemporaryDirectory() as cloned:
                clone = Path(cloned).resolve()
                for source in root.rglob('*'):
                    relative = source.relative_to(root).as_posix()
                    if source.is_file() and (not platform.excluded_path(relative, plan['excludePaths']) or relative == plan['finalPly']):
                        destination = clone / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, destination)
                compacted = platform.compact_state(clone)
                self.assertEqual((clone / compacted['finalPly']).read_bytes(), b'final' * 30)
                self.assertFalse((clone / snapshots[0]['path']).exists())
            plan = platform.compact_state(root)
            normalized = json.loads((root / 'platform-state.json').read_text())
            self.assertEqual(normalized['checkpoints'], [snapshots[-1]])
            self.assertEqual((root / plan['finalPly']).read_bytes(), b'final' * 30)
            self.assertEqual((training / 'final/config.json').read_bytes(), b'{"iterations":200}')
            self.assertTrue((root / snapshots[0]['path']).exists(), 'Offline compaction must never delete original bytes')
            files = normalized['completed']['training']['files']
            self.assertFalse(any(platform.excluded_path(item['path'], plan['excludePaths']) for item in files))
            self.assertIn(plan['finalPly'], [item['path'] for item in files])
            self.assertEqual(platform.compact_state(root)['retainedFiles'], plan['retainedFiles'])

    def test_spirula_final_checkpoint_and_ply_survive_acknowledged_compaction(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(platform, 'runtime_versions', return_value={}), \
                patch.object(platform, 'PROTOCOL_STREAM', io.StringIO()):
            request = self.request(Path(temporary))
            request['archiveAck'] = True
            runner = platform.Runner(request)
            acknowledged = []
            def complete(folder):
                for step in (20, 100):
                    checkpoint = folder / 'run' / f'step-{step:09}.ckpt'
                    checkpoint.mkdir(parents=True)
                    (checkpoint / 'splat.ply').write_bytes(f'PLY step {step}'.encode())
                    with tarfile.open(checkpoint / 'state.tar', 'w') as archive:
                        for name, content in {'state.json': b'{"full_resume":true,"cur_num_splats":3}',
                                              'world.means.npy': str(step).encode()}.items():
                            info = tarfile.TarInfo(name)
                            info.size = len(content)
                            archive.addfile(info, io.BytesIO(content))
                (folder / 'run/config.json').write_bytes(b'{"cap_max":3}')
                # Exercise offline selection of a final checkpoint not yet in state.
                old_state = dict(runner.state, completed={'training': dict(directory=folder.name, files=[])} )
                plan = platform.training_retention_plan(runner.output, old_state)
                self.assertTrue(plan['latestCheckpoint']['path'].endswith('step-000000100.ckpt'))
            def ack(stage):
                acknowledged.append(stage)
                if len(acknowledged) == 1:
                    self.assertTrue(list(runner.output.glob('*/run/step-*.ckpt')))
            runner.checkpoint = ack
            folder = runner.stage('training', complete)
            self.assertEqual((folder / 'final/splat.ply').read_bytes(), b'PLY step 100')
            self.assertFalse(list((folder / 'run').glob('step-*.ckpt')))
            self.assertEqual(len(runner.state['checkpoints']), 1)
            self.assertTrue(runner.state['checkpoints'][0]['path'].endswith('step-000000100.ckpt'))
            for item in runner.state['completed']['training']['files']:
                self.assertTrue(runner.path(item['path']).is_file())
            request.update(resume=True, archiveAck=False)
            platform.Runner(request)

    def test_densification_relocation_survives_multiple_scratch_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'densification-unique'
            dataset = folder.with_name(folder.name + '-dataset')
            folder.mkdir()
            original = {'scene_root': r'C:\old\root\densification-unique',
                        'images_subdir': r'C:\old\root\densification-unique-dataset\images', 'cap': 3}
            # State may already name an intermediate root after a crash; native paths
            # remain independently recognizable by their immutable stage identity.
            native = folder / 'config.json'
            native.write_text(json.dumps(original))
            platform.relocate_densification_config(folder, dataset)
            config = json.loads(native.read_text())
            self.assertEqual(config['scene_root'], str(folder))
            self.assertEqual(config['images_subdir'], str(dataset / 'images'))
            self.assertEqual(config['cap'], 3)
            backups = list(folder.glob('config-before-relocation-*.json'))
            self.assertEqual(json.loads(backups[0].read_text()), original)
            platform.relocate_densification_config(folder, dataset)
            self.assertEqual(len(list(folder.glob('config-before-relocation-*.json'))), 1)
            config['images_subdir'] = '/another/stage/images'
            native.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, 'unexpected images_subdir'):
                platform.relocate_densification_config(folder, dataset)

    def test_protocol_events_bypass_engine_log_redirection(self):
        protocol, logs = io.StringIO(), io.StringIO()
        with patch.object(platform, 'PROTOCOL_STREAM', protocol), contextlib.redirect_stdout(logs):
            platform.emit('checkpoint', checkpointId='closed-native-checkpoint')
        self.assertEqual(json.loads(protocol.getvalue())['type'], 'checkpoint')
        self.assertEqual(logs.getvalue(), '')

    def test_legacy_publication_request_finishes_with_local_exports_only(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(platform, 'runtime_versions', return_value={}), \
                patch.object(platform, 'PROTOCOL_STREAM', io.StringIO()) as protocol:
            request = self.request(Path(temporary))
            request['publication'] = {'enabled': True}
            request['upload'] = True
            runner = platform.Runner(request)
            ply = runner.output / 'result.ply'
            ply.write_text('ply\nformat ascii 1.0\nelement vertex 3\nend_header\n')
            exports = runner.output / 'export-test'
            exports.mkdir()
            with zipfile.ZipFile(exports / 'result.sog', 'w') as archive:
                meta = {'count': 3}
                for field in ('means', 'scales', 'quats', 'sh0'):
                    meta[field] = {'files': [field + '.webp']}
                    archive.writestr(field + '.webp', b'texture')
                archive.writestr('meta.json', json.dumps(meta))
            spz(exports / 'result.spz')
            with patch.object(runner, 'spirula', return_value=ply), \
                    patch.object(runner, 'stage', return_value=exports), \
                    patch('urllib.request.urlopen') as network:
                runner.run()
                network.assert_not_called()
            events = [json.loads(line) for line in protocol.getvalue().splitlines()]
            self.assertEqual(events[-1]['type'], 'completed')
            self.assertEqual(events[-1]['gaussians'], 3)
            self.assertFalse(any(event['type'] == 'publication' for event in events))
            self.assertFalse((runner.output / 'upload-job.json').exists())

    def request(self, root):
        source = root / 'input.mp4'
        source.write_bytes(b'unchanging stored input')
        return dict(scanId='scan', attemptId='attempt', backend='spirula', inputPath=str(source),
                    outputPath=str(root / 'work'), settings={'training': {'iterations': 100}}, maxCap=3)

    def test_spz_rejects_cap_truncated_payload_and_crc(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'output.spz'
            spz(path)
            self.assertEqual(platform.spz_count(path, 3), 3)
            with self.assertRaisesRegex(ValueError, 'outside'):
                platform.spz_count(path, 2)
            with gzip.open(path, 'wb') as stream:
                stream.write(struct.pack('<III4B', 0x5053474e, 3, 3, 0, 12, 0, 0))
            with self.assertRaisesRegex(ValueError, 'truncated'):
                platform.spz_count(path, 3)
            spz(path)
            data = bytearray(path.read_bytes())
            data[-8] ^= 1
            path.write_bytes(data)
            with self.assertRaises(gzip.BadGzipFile):
                platform.spz_count(path, 3)

    def test_cap_and_engine_settings_do_not_silently_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            request = self.request(Path(temporary))
            request['maxCap'] = True
            with self.assertRaisesRegex(ValueError, 'positive integer'):
                platform.validate_request(request)
            request['maxCap'] = 3
            request['settings']['training']['cap_max'] = 99
            with self.assertRaisesRegex(ValueError, 'training'):
                platform.validate_request(request)

    def test_resume_verifies_files_identity_versions_and_relative_paths(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(platform, 'runtime_versions', return_value={'engine': 'pinned'}), contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            request = self.request(root)
            runner = platform.Runner(request)
            def complete(folder):
                folder.mkdir()
                (folder / 'state.tar').write_bytes(b'checkpoint')
            folder = runner.stage('training', complete)
            request['resume'] = True
            platform.Runner(request)
            request['maxCap'] = 2
            with self.assertRaisesRegex(ValueError, 'changed'):
                platform.Runner(request)
            request['maxCap'] = 3
            request['runtimeVersions'] = {'engine': 'different'}
            with self.assertRaisesRegex(ValueError, 'runtime is unavailable'):
                platform.Runner(request)
            request.pop('runtimeVersions')
            (folder / 'state.tar').write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'artifact changed'):
                platform.Runner(request)
            with self.assertRaisesRegex(ValueError, 'escapes'):
                runner.path('../input.mp4')

    def test_archive_failure_preserves_completed_stage_and_every_attempt(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(platform, 'runtime_versions', return_value={}), contextlib.redirect_stdout(io.StringIO()):
            request = self.request(Path(temporary))
            request.update(archiveAck=True, archiveTimeoutSeconds=0)
            runner = platform.Runner(request)
            def complete(folder):
                folder.mkdir()
                (folder / 'splat.ply').write_bytes(b'retained PLY')
            with self.assertRaises(TimeoutError):
                runner.stage('training', complete)
            self.assertTrue(list(runner.output.rglob('splat.ply')))
            state = json.loads(runner.state_path.read_text())
            self.assertIn('training', state['completed'])
            request.update(resume=True, archiveAck=False)
            resumed = platform.Runner(request)
            resumed.stage('training', lambda _: self.fail('Must not repeat completed compute'))

    def test_spirula_arguments_enforce_cap_retention_and_disable_viewer(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(platform, 'runtime_versions', return_value={}), contextlib.redirect_stdout(io.StringIO()):
            runner = platform.Runner(self.request(Path(temporary)))
            commands = []
            def execute(command, log):
                args = [str(x) for x in command]
                commands.append(args)
                log.write_text('retained tool log')
                if args[1:3] == ['sam', 'extract']:
                    output = Path(args[args.index('--out') + 1]); output.mkdir()
                    for name in ('1.png', '2.png', '3.png'): (output / name).write_bytes(b'image')
                elif args[1:3] == ['sfm', 'auto']:
                    output = Path(args[args.index('-o') + 1]); output.mkdir()
                    for name in ('cameras.bin', 'points3D.bin'): (output / name).write_bytes(b'model')
                else:
                    output = Path(args[args.index('--output-dir-prefix') + 1]) / 'run/step-000000100.ckpt'
                    output.mkdir(parents=True)
                    (output / 'splat.ply').write_bytes(b'ply')
                    (output / 'state.tar').write_bytes(b'optimizer')
            runner.command = execute
            self.assertTrue(runner.spirula().is_file())
            train = commands[-1]
            for name, value in [('cap-max', '3'), ('save-full-checkpoint', '1'),
                                ('save-only-latest-checkpoint', '0'), ('disable-viewer', '1'),
                                ('keep-viewer-alive', '0'), ('num-iterations', '100')]:
                self.assertEqual(train[train.index('--' + name) + 1], value)

    def test_local_gpu_wait_serializes_runners(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = dict(os.environ, GPU_LOCK_PATH=str(root / 'gpu.lock'),
                       GPU_LOCK_WAIT='1', WORKER_MODE='native-development')
            env.pop('WORKER_PARENT_PID', None)
            script = ('import platform_runner as p,sys; from pathlib import Path; '
                      'lock=p.gpu_lock(); lock.__enter__(); '
                      'Path(sys.argv[1]).write_text("acquired"); '
                      'sys.stdin.readline(); lock.__exit__(None,None,None)')
            processes = []
            def wait_for(file):
                deadline = time.monotonic() + 10
                while not file.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(file.exists(), 'Runner did not acquire the GPU lock')
            try:
                first = subprocess.Popen([sys.executable, '-c', script, str(root / 'first')],
                                         env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
                processes.append(first)
                wait_for(root / 'first')
                second = subprocess.Popen([sys.executable, '-c', script, str(root / 'second')],
                                          env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True)
                processes.append(second)
                time.sleep(.5)
                self.assertIsNone(second.poll())
                self.assertFalse((root / 'second').exists())
                _, error = first.communicate('\n', timeout=10)
                self.assertEqual(first.returncode, 0, error)
                wait_for(root / 'second')
                output, error = second.communicate('\n', timeout=10)
                self.assertEqual(second.returncode, 0, error)
                self.assertIn('Waiting for another worktree', output)
            finally:
                for child in processes:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=10)

    @unittest.skipIf(os.name == 'nt', 'Linux worker lock semantics')
    def test_gpu_lock_survives_supervisor_exit_until_inherited_child_exits(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = dict(os.environ, GPU_LOCK_PATH=str(Path(temporary) / 'gpu.lock'))
            script = ('import platform_runner as p,subprocess,sys,os; '
                      'lock=p.gpu_lock(); lock.__enter__(); '
                      'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(1)"],'
                      'pass_fds=(int(os.environ["PIPELINE_GPU_LOCK_FD"]),)); '
                      'print(child.pid,flush=True); os._exit(0)')
            parent = subprocess.Popen([sys.executable, '-c', script], env=env, stdout=subprocess.PIPE, text=True)
            parent.stdout.readline()
            parent.wait(timeout=5)
            parent.stdout.close()
            with patch.dict(os.environ, env), self.assertRaisesRegex(RuntimeError, 'still owned'):
                with platform.gpu_lock():
                    self.fail('Inherited child must fence a replacement worker')

    @unittest.skipUnless(os.name == 'nt', 'Windows native development containment')
    def test_windows_abrupt_runner_death_kills_engine_descendant(self):
        import psutil
        with tempfile.TemporaryDirectory() as temporary:
            env = dict(os.environ, WORKER_MODE='native-development', GPU_LOCK_PATH=str(Path(temporary) / 'gpu.lock'))
            env.pop('WORKER_PARENT_PID', None)
            script = ('import platform_runner as p,subprocess,sys,time; '
                      'lock=p.gpu_lock(); lock.__enter__(); '
                      'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); '
                      'print(child.pid,flush=True); time.sleep(60)')
            parent = subprocess.Popen([sys.executable, '-c', script], env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                line = parent.stdout.readline()
                self.assertTrue(line.strip(), parent.stderr.read() if parent.poll() is not None else 'No child PID')
                child = psutil.Process(int(line.strip()))
                parent.kill()
                parent.wait(timeout=5)
                child.wait(timeout=5)
                self.assertFalse(child.is_running())
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
                parent.stdout.close()
                parent.stderr.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows native development parent watchdog')
    def test_windows_worker_parent_death_terminates_runner_job(self):
        import psutil
        with tempfile.TemporaryDirectory() as temporary:
            worker = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], creationflags=subprocess.CREATE_NO_WINDOW)
            env = dict(os.environ, WORKER_MODE='native-development', WORKER_PARENT_PID=str(worker.pid),
                       GPU_LOCK_PATH=str(Path(temporary) / 'gpu.lock'))
            script = ('import platform_runner as p,subprocess,sys,time; '
                      'lock=p.gpu_lock(); lock.__enter__(); '
                      'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); '
                      'print(child.pid,flush=True); time.sleep(60)')
            runner = subprocess.Popen([sys.executable, '-c', script], env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                child = psutil.Process(int(runner.stdout.readline().strip()))
                worker.kill(); worker.wait(timeout=5)
                runner.wait(timeout=5)
                child.wait(timeout=5)
                self.assertNotEqual(runner.returncode, 0)
                self.assertFalse(child.is_running())
            finally:
                for process in (worker, runner):
                    if process.poll() is None:
                        process.kill(); process.wait(timeout=5)
                runner.stdout.close(); runner.stderr.close()

    @unittest.skipIf(os.name == 'nt', 'Linux parent-death signal')
    def test_linux_worker_death_kills_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = dict(os.environ, GPU_LOCK_PATH=str(Path(temporary) / 'gpu.lock'))
            child_script = ('import platform_runner as p,os,time; '
                            'lock=p.gpu_lock();lock.__enter__(); '
                            'print(os.getpid(),flush=True);time.sleep(60)')
            worker_script = ('import os,subprocess,sys,time; '
                             'os.environ["WORKER_PARENT_PID"]=str(os.getpid()); '
                             'subprocess.Popen([sys.executable,"-c",sys.argv[1]]);time.sleep(60)')
            worker = subprocess.Popen([sys.executable, '-c', worker_script, child_script], env=env,
                                      stdout=subprocess.PIPE, text=True)
            try:
                pid = int(worker.stdout.readline().strip())
                worker.kill(); worker.wait(timeout=5)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = Path(f'/proc/{pid}/stat')
                    if not status.exists() or status.read_text().split()[2] == 'Z':
                        break
                    time.sleep(.05)
                else:
                    self.fail('Runner survived its worker')
            finally:
                if worker.poll() is None:
                    worker.kill(); worker.wait(timeout=5)
                worker.stdout.close()

    @unittest.skipIf(os.name == 'nt', 'Linux parent-death signal')
    def test_linux_supervisor_death_kills_engine_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / 'child.log'
            env = dict(os.environ, GPU_LOCK_PATH=str(root / 'gpu.lock'))
            script = ('import platform_runner as p,pipeline_common as c,sys; from pathlib import Path; '
                      'lock=p.gpu_lock();lock.__enter__();'
                      'c.run_logged([sys.executable,"-c","import os,time;print(os.getpid(),flush=True);time.sleep(60)"],Path(sys.argv[1]))')
            parent = subprocess.Popen([sys.executable, '-c', script, str(log)], env=env, stdout=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while (not log.exists() or not log.read_text().strip()) and time.monotonic() < deadline:
                    time.sleep(.05)
                pid = int(log.read_text().strip())
                parent.kill(); parent.wait(timeout=5)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = Path(f'/proc/{pid}/stat')
                    if not status.exists() or status.read_text().split()[2] == 'Z':
                        break
                    time.sleep(.05)
                else:
                    self.fail('GPU engine survived its supervisor')
            finally:
                if parent.poll() is None:
                    parent.kill(); parent.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
