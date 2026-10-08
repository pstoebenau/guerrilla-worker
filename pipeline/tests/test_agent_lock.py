import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class AgentLockTests(unittest.TestCase):
    def test_duplicate_agent_fails_before_jobs_and_lock_releases_after_exit(self):
        helper = Path(__file__).resolve().parents[1] / 'agent_lock.py'
        with tempfile.TemporaryDirectory() as folder:
            command = [sys.executable, str(helper), str(Path(folder) / 'gpu.lock'), str(os.getpid())]
            first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(first.stdout.readline().strip(), 'locked')
                duplicate = subprocess.run(command, capture_output=True, text=True, timeout=10)
                self.assertNotEqual(duplicate.returncode, 0)
                self.assertNotIn('locked', duplicate.stdout)
            finally:
                first.kill()
                first.communicate(timeout=10)
            second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(second.stdout.readline().strip(), 'locked')
            finally:
                second.kill()
                second.communicate(timeout=10)
