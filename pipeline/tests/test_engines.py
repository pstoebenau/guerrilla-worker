"""Exercise the adapter boundary with a third engine and real runner state."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from engine import Checkpoint, TrainingOutput
from engines import ENGINES
from engine_lichtfeld import LichtFeldEngine
import platform_runner as platform


class StubEngine:
    name = 'test-engine'
    checkpoint_suffix = '.recovery'
    requires_cuda = False

    def __init__(self):
        self.runs = 0
        self.version = 'v1'

    def validate_settings(self, settings, cap):
        if settings != {'training': {'iterations': 2}}:
            raise ValueError('Unsupported test engine settings')

    def runtime_versions(self, recorded):
        return {'testEngine': self.version}

    def preflight(self):
        return [dict(id='gpu-test', name='Test hardware', memoryBytes=1024)]

    def checkpoint(self, path):
        # Neither the suffix nor filename layout is known to the runner.
        return Checkpoint(path, path, path.with_suffix('.ply'), int(path.stem.split('-')[-1]))

    def checkpoint_candidates(self, folder):
        return (self.checkpoint(path) for path in folder.rglob('*.recovery'))

    def valid_checkpoint(self, checkpoint, cap):
        return checkpoint.source.read_bytes() == b'complete recovery state'

    def training_output(self, folder):
        return TrainingOutput(folder / 'final/scene.ply' if (folder / 'final').exists() else folder / 'scene.ply')

    def preview_ply(self, source, temporary, log):
        return source

    def run(self, runner):
        self.runs += 1
        def train(folder):
            folder.mkdir()
            (folder / 'scene.ply').write_text('ply\nformat ascii 1.0\nelement vertex 3\nend_header\n')
            for step in (1, 2):
                (folder / f'iteration-{step}.recovery').write_bytes(b'complete recovery state')
        folder = runner.stage('training', train, resumable=True)
        return self.training_output(folder).ply


def export_sog(_ply, target, *_args, **_kwargs):
    with zipfile.ZipFile(target, 'w') as archive:
        metadata = {'count': 3}
        for field in ('means', 'scales', 'quats', 'sh0'):
            metadata[field] = {'files': [field + '.webp']}
            archive.writestr(field + '.webp', b'texture')
        archive.writestr('meta.json', json.dumps(metadata))


class EngineTests(unittest.TestCase):
    def test_lichtfeld_runtime_identity_ignores_other_installed_engine_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'lichtfeld'
            executable.write_bytes(b'engine')
            engine = LichtFeldEngine()
            recorded = {'lichtfeldArtifactSha256': 'own-artifact', 'reconstructionEngine': 'pycolmap',
                        'spirulaArtifactSha256': 'spirula-v1', 'futureEngineArtifactSha256': 'future-v1'}
            with patch.object(platform.common, 'studio_path', return_value=executable), \
                    patch('engine_lichtfeld.subprocess.check_output', return_value='lichtfeld-v1'):
                before = engine.runtime_versions(recorded)
                recorded.update(spirulaArtifactSha256='spirula-v2', futureEngineArtifactSha256='future-v2')
                self.assertEqual(engine.runtime_versions(recorded), before)
            self.assertEqual(before['lichtfeldArtifactSha256'], 'own-artifact')
            self.assertEqual(before['reconstructionEngine'], 'pycolmap')
            self.assertNotIn('futureEngineArtifactSha256', before)

    def request(self, root):
        source = root / 'input.mp4'
        source.write_bytes(b'input')
        return dict(scanId='scan', attemptId='attempt', backend='test-engine', maxCap=3,
                    inputPath=str(source), outputPath=str(root / 'run'), settings={'training': {'iterations': 2}})

    def test_injected_third_engine_runs_retains_compacts_and_resumes_without_core_changes(self):
        engine = StubEngine()
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(platform.common, 'converter_version', return_value='converter-v1'), \
                patch.object(platform.common, 'export_sog', side_effect=export_sog), \
                patch.object(platform, 'PROTOCOL_STREAM', io.StringIO()), \
                patch.dict(ENGINES, {engine.name: engine}):
            request = self.request(Path(temporary))
            runner = platform.Runner(request, engine=engine)
            runner.run()
            self.assertEqual(runner.state['result']['gaussians'], 3)
            self.assertEqual(engine.runs, 1)
            latest = runner.path(runner.state['checkpoints'][0]['path'])
            self.assertEqual(latest.name, 'iteration-2.recovery')
            self.assertTrue(latest.is_file())
            plan = platform.compact_state(runner.output)
            self.assertEqual(plan['latestCheckpoint']['path'], latest.relative_to(runner.output).as_posix())
            self.assertTrue(any(path.endswith('iteration-1.recovery') for path in plan['excludePaths']))
            self.assertEqual(Path(plan['finalPly']).name, 'scene.ply')
            request['resume'] = True
            platform.Runner(request, engine=engine).run()
            self.assertEqual(engine.runs, 1, 'Completed export must skip the engine')
            engine.version = 'v2'
            with self.assertRaisesRegex(ValueError, 'versions changed'):
                platform.Runner(request, engine=engine)

    def test_request_rejects_mismatched_adapter_and_engine_specific_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            request = self.request(Path(temporary))
            with self.assertRaisesRegex(ValueError, 'does not match'):
                platform.validate_request(request, engine=ENGINES['spirula'])
            request['settings'] = {'training': {'iterations': 9}}
            with self.assertRaisesRegex(ValueError, 'test engine settings'):
                platform.validate_request(request, engine=StubEngine())
            request['settings'] = {'training': None}
            with self.assertRaisesRegex(ValueError, 'objects'):
                platform.validate_request(request, engine=StubEngine())

    def test_preflight_reports_individual_versions_and_isolates_a_failed_engine(self):
        healthy, missing = StubEngine(), StubEngine()
        missing.name = 'missing-engine'
        def unavailable():
            raise RuntimeError('Engine is not installed')
        missing.preflight = unavailable
        with patch.object(platform.common, 'converter_version', return_value='converter-v1'):
            report = platform.preflight((healthy, missing))
        self.assertTrue(report['backends'][healthy.name]['available'])
        self.assertEqual(report['backends'][healthy.name]['runtimeVersions']['testEngine'], 'v1')
        self.assertFalse(report['backends'][missing.name]['available'])
        self.assertIn('not installed', report['backends'][missing.name]['error'])
        self.assertEqual(report['gpus'], healthy.preflight())
        self.assertNotIn('runtimeVersions', report, 'Runtime identities belong to individual engines')

    def test_job_startup_preflights_only_the_requested_engine(self):
        engine = StubEngine()
        with tempfile.TemporaryDirectory() as temporary:
            request = self.request(Path(temporary))
            file = Path(temporary) / 'request.json'
            file.write_text(json.dumps(request))
            with patch.dict(ENGINES, {engine.name: engine}), \
                    patch.object(platform, 'gpu_lock', return_value=contextlib.nullcontext()), \
                    patch.object(platform, 'preflight', return_value={'backends': {engine.name: {'available': True}}}) as preflight, \
                    patch.object(platform, 'Runner') as runner:
                self.assertEqual(platform.main(['--request', str(file)]), 0)
            preflight.assert_called_once_with((engine,))
            runner.return_value.run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
