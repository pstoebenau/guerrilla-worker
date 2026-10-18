import json
import os
from pathlib import Path
import tempfile
import types
import textwrap
import unittest
from unittest.mock import patch


class RuntimeWorkflowTests(unittest.TestCase):
    def test_setup_reuses_offline_runtime_and_checks_version_with_a_deadline(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/worker.yml').read_text()
        step = workflow.split('      - name: Verify native runtime installation and offline reuse\n', 1)[1]
        script = textwrap.dedent(step.split("          python - <<'PYTHON'\n", 1)[1].split('          PYTHON\n', 1)[0])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = root / 'installed'
            installed.mkdir()
            engine = installed / 'spirula'
            model = installed / 'romav2.0.1.pt'
            engine.write_bytes(b'executable fixture')
            model.write_bytes(b'model fixture')
            (installed / 'manifest.json').write_text(json.dumps({'version': '2026.10.16'}))
            result = {'type': 'runtime-setup-complete', 'root': str(installed),
                      'spirulaPath': str(engine), 'modelPath': str(model)}
            calls = []

            def run(command, **options):
                calls.append((command, dict(options), dict(options['env'])))

            def output(command, **options):
                calls.append((command, dict(options), dict(options['env'])))
                if command[-1] == '--version':
                    return '2026.10.16 (release-commit)\n'
                return json.dumps(result) + '\n'

            for platform, separator in [('posix', ':'), ('nt', ';')]:
                with self.subTest(platform=platform):
                    calls.clear()
                    platform_os = types.SimpleNamespace(name=platform, pathsep=separator,
                        environ=dict(os.environ, RUNNER_TEMP=str(root / 'temporary'), PATH='ambient-tools'))
                    with patch.object(Path, 'cwd', return_value=root), \
                            patch.dict('sys.modules', {'os': platform_os}), \
                            patch('subprocess.run', side_effect=run), \
                            patch('subprocess.check_output', side_effect=output):
                        exec(compile(script, 'worker.yml runtime verification', 'exec'), {})
                    self.assertEqual(len(calls), 3)
                    self.assertEqual(calls[0][0][-1], 'setup-runtime')
                    self.assertEqual(calls[1][2]['HTTPS_PROXY'], 'http://127.0.0.1:1')
                    self.assertEqual(calls[2][0], [str(engine), '--version'])
                    self.assertEqual(calls[2][1]['timeout'], 30)
                    for _, options, environment in calls:
                        self.assertGreater(options['timeout'], 0)
                        if platform == 'nt':
                            first = environment['PATH'].split(separator)[0]
                            self.assertEqual(first, str(root / 'desktop/src-tauri/resources/runtime/windows-crt'))
                        else:
                            self.assertEqual(environment['PATH'], 'ambient-tools')



if __name__ == '__main__':
    unittest.main()
