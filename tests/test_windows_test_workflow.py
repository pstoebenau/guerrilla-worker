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
                self.assertEqual(len(run.call_args_list), 3)
                self.assertEqual(run.call_args_list[0].args[0][1], '/dependents')
                self.assertEqual(run.call_args_list[1].args[0][1], '/imports:comctl32.dll')
                self.assertEqual(run.call_args_list[2].args[0][1], '/headers')
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
