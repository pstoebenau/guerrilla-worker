import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import runtime_setup


class RuntimeSetupTests(unittest.TestCase):
    def tearDown(self):
        runtime_setup.cancelled.clear()

    def test_canceling_setup_terminates_its_subprocess_tree(self):
        try:
            import psutil
        except ImportError:
            self.skipTest('Verified with the packaged Python runtime, which includes psutil')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pid_file = root / 'child.pid'
            script = ('import subprocess, sys, time; from pathlib import Path; '
                      'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); '
                      f'Path({str(pid_file)!r}).write_text(str(child.pid)); '
                      'time.sleep(60)')
            failures = []
            def run():
                try:
                    runtime_setup.run_command([sys.executable, '-c', script], root / 'setup.log', 'fixture', os.environ.copy())
                except RuntimeError as error:
                    failures.append(str(error))
            thread = threading.Thread(target=run)
            thread.start()
            try:
                import time
                deadline = time.monotonic() + 10
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pid_file.exists(), 'fixture subprocess did not start')
                descendant = psutil.Process(int(pid_file.read_text()))
                runtime_setup.cancelled.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, ['GPU setup canceled; verified downloads remain cached'])
                self.assertTrue(not descendant.is_running() or descendant.status() == psutil.STATUS_ZOMBIE)
            finally:
                runtime_setup.cancelled.set()
                thread.join(5)

    def test_verified_download_cache_is_reusable_offline_and_corruption_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            payload = b'pinned download bytes'
            asset = {'url': 'https://example.invalid/runtime', 'sha256': hashlib.sha256(payload).hexdigest()}
            file = cache / asset['sha256']
            file.write_bytes(payload)
            with patch.object(runtime_setup.urllib.request, 'build_opener', side_effect=AssertionError('No network for a verified cached file')):
                self.assertEqual(runtime_setup.download(asset, cache, 'fixture'), file)
            file.write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'checksum verification'):
                runtime_setup.download(asset, cache, 'fixture')
            with self.assertRaisesRegex(ValueError, 'HTTPS'):
                runtime_setup.download({**asset, 'url': 'http://example.invalid/runtime'}, cache, 'fixture')

    def test_tampered_download_never_becomes_a_cached_verified_asset(self):
        class Response(io.BytesIO):
            headers = {'Content-Length': '6'}
        class Opener:
            def open(self, *args, **kwargs):
                return Response(b'wrong!')
        with tempfile.TemporaryDirectory() as folder, patch.object(runtime_setup.urllib.request, 'build_opener', return_value=Opener()):
            cache = Path(folder)
            with self.assertRaisesRegex(ValueError, 'download refused'):
                runtime_setup.download({'url': 'https://example.invalid/runtime', 'sha256': 'a' * 64}, cache, 'fixture')
            self.assertFalse(list(cache.iterdir()))

    def test_archive_paths_cannot_escape_the_owned_install_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sentinel = root / 'checkpoint'
            sentinel.write_text('preserved')
            python = root / 'python.tar.gz'
            with tarfile.open(python, 'w:gz') as archive:
                file = tarfile.TarInfo('python/../../checkpoint')
                file.size = 7
                archive.addfile(file, io.BytesIO(b'changed'))
            install = root / 'install'
            install.mkdir()
            with self.assertRaises(tarfile.OutsideDestinationError):
                runtime_setup.extract_python(python, install)
            source = root / 'source.zip'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('facebookresearch-dinov3-fixture/../../checkpoint', 'changed')
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                runtime_setup.extract_dinov3(source, install)
            self.assertEqual(sentinel.read_text(), 'preserved')

    def test_incomplete_mismatched_or_tampered_runtime_is_not_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            python = runtime_setup.python_binary(root)
            python.parent.mkdir(parents=True)
            python.write_bytes(b'python')
            descriptor = {'identity': 'cuda-' + 'a' * 64, 'target': 'fixture',
                          'models': {'roma': {'sha256': hashlib.sha256(b'model').hexdigest()}, 'dinov3Commit': 'commit'}}
            manifest = {'identity': descriptor['identity'], 'target': 'other', 'pythonSha256': hashlib.sha256(b'python').hexdigest()}
            (root / 'manifest.json').write_text(json.dumps(manifest))
            self.assertFalse(runtime_setup.ready(root, descriptor))
            manifest['target'] = 'fixture'
            (root / 'manifest.json').write_text(json.dumps(manifest))
            self.assertFalse(runtime_setup.ready(root, descriptor))
            model = root / 'models/hub/checkpoints/romav2.pt'
            model.parent.mkdir(parents=True)
            model.write_bytes(b'model')
            (root / 'models/hub/facebookresearch_dinov3_commit').mkdir()
            self.assertTrue(runtime_setup.ready(root, descriptor, verify=True))
            model.write_bytes(b'corrupt')
            self.assertFalse(runtime_setup.ready(root, descriptor, verify=True))


if __name__ == '__main__':
    unittest.main()
