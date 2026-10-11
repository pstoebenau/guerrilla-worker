import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import psutil
from process_runtime import paused_engine


@unittest.skipIf(os.name == 'nt', 'POSIX process groups; Windows has Job Object tests')
class ProcessRuntimeTests(unittest.TestCase):
    def test_checkpoint_freezes_descendants_and_reports_their_open_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'writing.ckpt'
            child_script = 'import sys,time; f=open(sys.argv[1],"w");print("ready",flush=True);time.sleep(60)'
            parent_script = 'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]]);p.wait()'
            child = subprocess.Popen([sys.executable, '-c', parent_script, child_script, str(target)],
                                     start_new_session=True, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                with paused_engine(child) as files:
                    self.assertIn(target.resolve(), files)
                    self.assertEqual(psutil.Process(child.pid).status(), psutil.STATUS_STOPPED)
                self.assertNotEqual(psutil.Process(child.pid).status(), psutil.STATUS_STOPPED)
            finally:
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate(timeout=5)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS parent watchdog')
    def test_abrupt_runner_death_stops_engine_and_releases_inherited_gpu_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / 'engine.log'
            env = dict(os.environ, GPU_LOCK_PATH=str(root / 'gpu.lock'))
            env.pop('WORKER_PARENT_PID', None)
            script = ('from process_runtime import gpu_lock; from pipeline_common import run_logged; '
                      'from pathlib import Path; import sys; '
                      'lock=gpu_lock();lock.__enter__();'
                      'run_logged([sys.executable,"-c","import os,time;print(os.getpid(),flush=True);time.sleep(60)"],Path(sys.argv[1]))')
            parent = subprocess.Popen([sys.executable, '-c', script, str(log)], env=env, stdout=subprocess.DEVNULL)
            engine = None
            try:
                deadline = time.monotonic() + 5
                while (not log.exists() or not log.read_text().strip()) and time.monotonic() < deadline:
                    time.sleep(.05)
                engine = psutil.Process(int(log.read_text().strip()))
                parent.kill(); parent.wait(timeout=5)
                engine.wait(timeout=5)
                from process_runtime import gpu_lock
                from unittest.mock import patch
                # The guard reaps the engine before releasing its own inherited
                # descriptor. Wait for that cleanup, with a bounded deadline.
                deadline = time.monotonic() + 5
                while True:
                    try:
                        with patch.dict(os.environ, env), gpu_lock():
                            break
                    except RuntimeError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(.05)
            finally:
                if parent.poll() is None:
                    parent.kill(); parent.wait(timeout=5)
                if engine and engine.is_running():
                    engine.kill()


if __name__ == '__main__':
    unittest.main()
