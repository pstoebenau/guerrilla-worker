"""Hold an OS-backed per-GPU lock while the enrolled agent remains alive."""
import os
import sys
import time
from pathlib import Path
from filelock import FileLock

lock = Path(sys.argv[1])
lock.parent.mkdir(parents=True, exist_ok=True)
parent = int(sys.argv[2])
with FileLock(lock, timeout=0):
    print('locked', flush=True)
    while True:
        if os.name == 'nt':
            import psutil
            if not psutil.pid_exists(parent):
                break
        else:
            try:
                os.kill(parent, 0)
            except ProcessLookupError:
                break
        time.sleep(1)
