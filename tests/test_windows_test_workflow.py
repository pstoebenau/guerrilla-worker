import hashlib
import json
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def workflow_script(name):
    step = (ROOT / '.github/workflows/worker.yml').read_text().split(f'      - name: {name}\n', 1)[1]
    return textwrap.dedent(step.split("          python - <<'PYTHON'\n", 1)[1].split('          PYTHON\n', 1)[0])


class WindowsDesktopTestWorkflowTests(unittest.TestCase):
    def fixture(self, root):
        source = root / 'desktop/src-tauri/resources/runtime/windows-crt'
        source.mkdir(parents=True)
        records = []
        for name in ['msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'concrt140.dll']:
            payload = f'verified Microsoft fixture {name}'.encode()
            (source / name).write_bytes(payload)
            records.append({'fileName': name, 'sha256': hashlib.sha256(payload).hexdigest(),
                            'sourceFileVersion': '14.50.12345.0'})
        (source / 'manifest.json').write_text(json.dumps({'schemaVersion': 1, 'architecture': 'x64', 'files': records}))
        return source

    def test_verified_packaged_crt_is_staged_beside_cargo_test_executables(self):
        script = workflow_script('Stage Windows app-local CRT beside desktop Rust tests')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.fixture(root)
            destination = root / 'desktop/src-tauri/target/release/deps'
            destination.mkdir(parents=True)
            (destination / 'msvcp140.dll').write_bytes(b'old incompatible copy')
            (destination / 'guerrilla_worker_desktop-fixture.exe').write_bytes(b'test executable')
            with patch.object(Path, 'cwd', return_value=root):
                exec(compile(script, 'worker.yml Windows test CRT staging', 'exec'), {})
            for dll in source.glob('*.dll'):
                self.assertEqual((destination / dll.name).read_bytes(), dll.read_bytes())
            self.assertEqual((destination / 'guerrilla_worker_desktop-fixture.exe').read_bytes(), b'test executable')
            workflow = (ROOT / '.github/workflows/worker.yml').read_text()
            self.assertLess(workflow.index('name: Stage Windows app-local CRT'), workflow.index('name: Test the built worker'))
            self.assertIn("if: runner.os == 'Windows'", workflow.split('name: Stage Windows app-local CRT', 1)[1].split('      - name:', 1)[0])

    def test_corrupt_or_incomplete_crt_is_rejected_before_staging(self):
        script = workflow_script('Stage Windows app-local CRT beside desktop Rust tests')
        for mode in ('corrupt', 'missing', 'traversal'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = self.fixture(root)
                manifest_path = source / 'manifest.json'
                manifest = json.loads(manifest_path.read_text())
                if mode == 'corrupt':
                    (source / 'msvcp140.dll').write_bytes(b'tampered')
                elif mode == 'missing':
                    manifest['files'].pop(0)
                else:
                    manifest['files'].append({'fileName': '../outside.dll', 'sha256': '0' * 64})
                manifest_path.write_text(json.dumps(manifest))
                with patch.object(Path, 'cwd', return_value=root), self.assertRaises(ValueError):
                    exec(compile(script, 'worker.yml Windows test CRT staging', 'exec'), {})
                self.assertFalse((root / 'desktop/src-tauri/target/release/deps').exists())

    def test_failure_diagnostics_inspect_imports_without_suppressing_failed_tests(self):
        script = workflow_script('Diagnose Windows desktop test loader failures')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'desktop/src-tauri/target/release/deps/guerrilla_worker_desktop-fixture.exe'
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b'test executable')
            previous = Path.cwd()
            try:
                os.chdir(root)
                with patch('shutil.which', return_value='dumpbin.exe'), patch('subprocess.run') as run:
                    exec(compile(script, 'worker.yml Windows loader diagnostics', 'exec'), {})
                self.assertEqual(len(run.call_args_list), 4)
                self.assertEqual(run.call_args_list[0].args[0][1], '/dependents')
                self.assertTrue(all(call.kwargs['timeout'] == 30 for call in run.call_args_list))
            finally:
                os.chdir(previous)
        workflow = (ROOT / '.github/workflows/worker.yml').read_text()
        diagnostic = workflow.split('name: Diagnose Windows desktop test loader failures', 1)[1].split('      - name:', 1)[0]
        self.assertIn("if: failure() && runner.os == 'Windows'", diagnostic)
        self.assertIn('cargo test --release --locked --workspace', workflow)
        self.assertIn('cargo test --release --locked --manifest-path desktop/src-tauri/Cargo.toml --target-dir desktop/src-tauri/target --features tauri/custom-protocol', workflow)


if __name__ == '__main__':
    unittest.main()
