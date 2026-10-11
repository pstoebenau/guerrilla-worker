"""OS process primitives shared by every scan backend.

Keep containment, checkpoint freezing and inherited GPU locks here. Engine
adapters operate on commands and artifacts, independently of the host OS.
"""
import contextlib
import ctypes
import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

@contextlib.contextmanager
def paused_engine(process):
    if os.name == 'nt':
        import psutil
        root = psutil.Process(process.pid)
        stopped = []
        try:
            root.suspend()
            stopped.append(root)
            for child in root.children(recursive=True):
                child.suspend()
                stopped.append(child)
            yield {Path(item.path).resolve() for child in stopped for item in child.open_files()}
        finally:
            for child in reversed(stopped):
                try:
                    child.resume()
                except psutil.NoSuchProcess:
                    pass
        return
    os.killpg(process.pid, signal.SIGSTOP)
    try:
        os.waitpid(process.pid, os.WUNTRACED)
        import psutil
        root = psutil.Process(process.pid)
        children = [root, *root.children(recursive=True)]
        deadline = time.monotonic() + 5
        while any(child.status() != psutil.STATUS_STOPPED for child in children):
            if time.monotonic() >= deadline:
                raise RuntimeError('Could not freeze every engine process for a checkpoint')
            time.sleep(.01)
        yield {Path(item.path).resolve() for child in children for item in child.open_files()}
    finally:
        try:
            os.killpg(process.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass


@contextlib.contextmanager
def gpu_lock(on_wait=None):
    """The lock survives a killed supervisor while its engine descendants live."""
    wait = os.environ.get('GPU_LOCK_WAIT') == '1'
    announced = False

    def wait_for_owner():
        nonlocal announced
        if not announced:
            if on_wait:
                on_wait()
            else:
                print('Waiting for another worktree to release the GPU', flush=True)
            announced = True
        if os.name == 'nt' and os.environ.get('WORKER_PARENT_PID'):
            import psutil
            if not psutil.pid_exists(int(os.environ['WORKER_PARENT_PID'])):
                raise InterruptedError('Native worker exited while waiting for the GPU')
        time.sleep(0.25)

    if os.name == 'nt':
        import msvcrt
        from windows_job import WindowsJob
        path = Path(os.environ.get('GPU_LOCK_PATH', str(Path.home() / '.cache/guerrilla-worker/gpu.lock')))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+b') as stream:
            stream.write(b'0'); stream.flush(); stream.seek(0)
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if not wait or exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise RuntimeError('GPU is still owned by another native runner') from exc
                    wait_for_owner()
            containment = WindowsJob(path)
            try:
                yield
            finally:
                containment.close()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    worker_parent = os.environ.get('WORKER_PARENT_PID')
    if worker_parent and sys.platform == 'linux':
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        # Node can die without delivering its graceful shutdown signal. The
        # runner then unwinds run_logged, terminating and reaping engine groups.
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'Cannot bind runner lifetime to worker')
        if os.getppid() != int(worker_parent):
            raise RuntimeError('Worker parent no longer owns this runner')
    default_lock = str(Path.home() / '.cache/guerrilla-worker/gpu.lock')
    path = Path(os.environ.get('GPU_LOCK_PATH', default_lock))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if not wait:
                    raise RuntimeError('GPU still owned by another runner or surviving engine process') from exc
                wait_for_owner()
        old = os.environ.get('PIPELINE_GPU_LOCK_FD')
        os.environ['PIPELINE_GPU_LOCK_FD'] = str(stream.fileno())
        try:
            yield
        finally:
            if old is None:
                os.environ.pop('PIPELINE_GPU_LOCK_FD', None)
            else:
                os.environ['PIPELINE_GPU_LOCK_FD'] = old
            # Do not LOCK_UN: shared inherited descriptors must retain ownership.



def launch(command, **streams):
    options = dict(start_new_session=os.name != 'nt',
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if os.name != 'nt' and os.environ.get('PIPELINE_GPU_LOCK_FD'):
        options['pass_fds'] = (int(os.environ['PIPELINE_GPU_LOCK_FD']),)
    if sys.platform == 'darwin':
        command = [sys.executable, str(Path(__file__).with_name('parent_guard.py')), str(os.getpid()), *command]
    elif sys.platform == 'linux' and os.environ.get('PIPELINE_GPU_LOCK_FD'):
        libc = ctypes.CDLL(None, use_errno=True)
        parent_pid = os.getpid()
        def parent_death_signal():
            if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
                os._exit(1)
        options['preexec_fn'] = parent_death_signal
    return subprocess.Popen(command, **options, **streams)


def terminate(process, force=False):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass
