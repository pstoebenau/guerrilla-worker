"""macOS process-group supervisor: stop descendants even after abrupt parent exit.

The caller starts this helper in a new session. Children inherit its process
group and the GPU lock descriptor, so an orphan cannot overlap a replacement.
"""
import os
import signal
import subprocess
import sys
import time


def main():
    parent = int(sys.argv[1])
    if os.getpgrp() != os.getpid() or os.getppid() != parent:
        raise RuntimeError('Process guard requires a new session and its original parent')
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
    descriptors = (int(os.environ['PIPELINE_GPU_LOCK_FD']),) if os.environ.get('PIPELINE_GPU_LOCK_FD') else ()
    child = subprocess.Popen(sys.argv[2:], pass_fds=descriptors)
    while child.poll() is None:
        if stopping or os.getppid() != parent:
            # Our handler absorbs this signal; the engine and its descendants
            # receive it too. Escalation kills the whole group, including us.
            os.killpg(os.getpid(), signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpid(), signal.SIGKILL)
            return 1
        time.sleep(0.1)
    return child.returncode if child.returncode >= 0 else 1


if __name__ == '__main__':
    sys.exit(main())
