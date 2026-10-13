"""CPU checks of the Rust/Python boundary and native operation isolation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_bindings


class NativeBindingsTests(unittest.TestCase):
    def test_invalid_protocol_inputs_are_rejected_before_native_imports(self):
        for request in [{}, {'action': 1}, {'action': 'unknown'},
                        {'action': 'open-files', 'pids': [True]},
                        {'action': 'selection', 'source': '', 'output': 'out', 'options': {}},
                        {'action': 'selection', 'source': '/missing/source', 'output': 'out', 'options': {}}]:
            with self.subTest(request=request), self.assertRaises(ValueError):
                native_bindings.execute(request)

    def test_owner_engine_api_is_discovered_without_ambient_pythonpath(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = Path(temp) / 'installed-engine'
            ambient = Path(temp) / 'ambient'
            engine.mkdir()
            ambient.mkdir()
            (engine / 'lichtfeld.py').write_text('origin = "owner engine"')
            (ambient / 'lichtfeld.py').write_text('raise RuntimeError("ambient import override")')
            observed = []
            class EngineProbe:
                def preflight(self):
                    import lichtfeld
                    observed.append((lichtfeld.origin, Path(lichtfeld.__file__).parent))
            fake = types.ModuleType('engine_lichtfeld')
            fake.LichtFeldEngine = EngineProbe
            previous = sys.modules.pop('lichtfeld', None)
            try:
                with patch.object(sys, 'path', sys.path.copy()), patch.dict(os.environ, {'PYTHONPATH': str(ambient)}), patch.dict(sys.modules, {'engine_lichtfeld': fake}):
                    result = native_bindings.execute({'action': 'probe', 'engineBin': str(engine)})
                self.assertTrue(result['available'])
                self.assertEqual(observed, [('owner engine', engine.resolve())])
            finally:
                sys.modules.pop('lichtfeld', None)
                if previous is not None:
                    sys.modules['lichtfeld'] = previous

    def test_embedded_engine_python_abi_must_match_vendored_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, 'ABI differs'):
                native_bindings.execute({'action': 'unknown', 'pythonSitePackages': temp, 'pythonVersion': [1, 0]})

    def test_selection_uses_one_validated_native_operation(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'video.mp4'
            source.write_bytes(b'video')
            calls = []
            fake = types.ModuleType('select_frames')
            fake.main = lambda arguments: calls.append(arguments)
            with patch.dict(sys.modules, {'select_frames': fake}):
                result = native_bindings.execute({'action': 'selection', 'source': str(source), 'output': str(Path(temp) / 'frames'),
                    'options': {'max_images': 10, 'no_glare_filter': True, 'end_seconds': None}})
            self.assertEqual(result['output'], str(Path(temp) / 'frames'))
            self.assertEqual(len(calls), 1)
            self.assertIn('--max-images', calls[0])
            self.assertIn('--no-glare-filter', calls[0])
            self.assertNotIn('--end-seconds', calls[0])

    def test_colmap_existing_workspace_uses_native_identity_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            images = Path(temp) / 'images'
            output = Path(temp) / 'reconstruction'
            dataset = output / 'dataset'
            images.mkdir()
            dataset.mkdir(parents=True)
            (output / 'colmap-state.json').write_text('{"signature":"existing"}')
            calls = []
            fake = types.ModuleType('colmap_worker')
            fake.probe = lambda settings: None
            def reconstruct(source, target, settings, retain_artifacts):
                calls.append((source, target, settings, retain_artifacts))
                self.assertTrue((target / 'colmap-state.json').is_file())
                (target / 'colmap-result.json').write_text(json.dumps({'dataset_dir': str(dataset)}))
            fake.reconstruct = reconstruct
            with patch.dict(sys.modules, {'colmap_worker': fake}):
                native_bindings.execute({'action': 'reconstruction', 'images': str(images), 'output': str(output), 'options': {'mode': 'global'}})
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0][-1])

    def test_cli_returns_only_validated_json_on_stdout_and_nonzero_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            request = Path(temp) / 'request.json'
            request.write_text(json.dumps({'action': 'unknown'}))
            completed = subprocess.run([sys.executable, str(Path(native_bindings.__file__)), '--request', str(request)], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 1)
            report = json.loads(completed.stdout)
            self.assertEqual(report['version'], 1)
            self.assertFalse(report['ok'])
            self.assertIn('Unsupported native operation', report['error'])


if __name__ == '__main__':
    unittest.main()
