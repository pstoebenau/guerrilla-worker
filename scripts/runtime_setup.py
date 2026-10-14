"""Install the pinned GPU dependencies in an owned runtime, leaving the app unchanged."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

cancelled = threading.Event()


def check_cancelled():
    if cancelled.is_set():
        raise RuntimeError('GPU setup canceled; verified downloads remain cached')


def watch_parent(parent_pid):
    import psutil
    try:
        parent = psutil.Process(parent_pid)
    except psutil.NoSuchProcess:
        cancelled.set()
        return
    def monitor():
        while not cancelled.wait(0.25):
            if not parent.is_running():
                cancelled.set()
                return
    threading.Thread(target=monitor, daemon=True).start()


def digest(file):
    with Path(file).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def target():
    architecture = platform.machine().lower()
    return f'{sys.platform}-' + ('arm64' if architecture in ('arm64', 'aarch64') else 'x64' if architecture in ('amd64', 'x86_64') else architecture)


def event(message, **values):
    check_cancelled()
    print(json.dumps({'type': 'runtime-setup-progress', 'message': message, **values}), flush=True)


def secure_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('GPU runtime downloads require HTTPS without credentials')
    return url


class SecureRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, secure_url(newurl))


def download(asset, cache, label):
    check_cancelled()
    secure_url(asset['url'])
    if not re.fullmatch(r'[0-9a-f]{64}', asset['sha256']):
        raise ValueError('Invalid pinned GPU runtime checksum')
    cache.mkdir(parents=True, exist_ok=True)
    file = cache / asset['sha256']
    if file.is_file():
        if digest(file) != asset['sha256']:
            raise ValueError('Cached GPU runtime download failed checksum verification')
        return file
    opener = urllib.request.build_opener(SecureRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    temporary = cache / (asset['sha256'] + '.' + uuid.uuid4().hex + '.download')
    try:
        for attempt in range(4):
            try:
                request = urllib.request.Request(asset['url'], headers={'User-Agent': 'Guerrilla-Worker-Runtime-Setup'})
                with opener.open(request, timeout=60) as response, temporary.open('wb') as output:
                    total = int(response.headers.get('Content-Length', '0'))
                    received, last = 0, 0.0
                    while block := response.read(1024 * 1024):
                        check_cancelled()
                        output.write(block)
                        received += len(block)
                        if time.monotonic() - last > 1:
                            event(label, downloadedBytes=received, totalBytes=total)
                            last = time.monotonic()
                    output.flush()
                    os.fsync(output.fileno())
                break
            except (urllib.error.URLError, OSError, http.client.IncompleteRead) as error:
                if attempt == 3 or isinstance(error, urllib.error.HTTPError) and error.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f'{label} failed; check the connection and retry setup') from error
                event(f'{label} interrupted; retrying')
                cancelled.wait(2 ** attempt)
                check_cancelled()
        if digest(temporary) != asset['sha256']:
            raise ValueError(f'{label} failed checksum verification; download refused')
        check_cancelled()
        temporary.replace(file)
        return file
    finally:
        temporary.unlink(missing_ok=True)


def extract_python(archive, destination):
    with tarfile.open(archive, 'r:*') as source:
        if any(not member.name.startswith('python/') and member.name.rstrip('/') != 'python' for member in source.getmembers()):
            raise ValueError('Unexpected Python runtime archive layout')
        source.extractall(destination, filter='data')


def extract_dinov3(archive, destination):
    with zipfile.ZipFile(archive) as source:
        names = source.namelist()
        roots = {name.split('/')[0] for name in names}
        if len(roots) != 1 or not next(iter(roots)).startswith('facebookresearch-dinov3-'):
            raise ValueError('Unexpected DINOv3 source archive layout')
        for name in names:
            if '\\' in name or name.startswith('/') or '..' in Path(name).parts:
                raise ValueError('Unsafe DINOv3 source archive path')
        source.extractall(destination)
        return destination / next(iter(roots))


def python_binary(directory):
    return directory / 'python' / ('python.exe' if os.name == 'nt' else 'bin/python3')


def ready(directory, descriptor, verify=False):
    try:
        manifest = json.loads((directory / 'manifest.json').read_text())
        python = python_binary(directory)
        model = directory / 'models/hub/checkpoints/romav2.pt'
        source = directory / 'models/hub' / ('facebookresearch_dinov3_' + descriptor['models']['dinov3Commit'])
        if directory.is_symlink() or manifest['identity'] != descriptor['identity'] or manifest['target'] != descriptor['target'] or not python.is_file() or not model.is_file() or not source.is_dir():
            return False
        return not verify or (digest(python) == manifest['pythonSha256'] and digest(model) == descriptor['models']['roma']['sha256'])
    except (OSError, KeyError, ValueError):
        return False


def run_command(arguments, log, label, environment):
    import psutil
    event(label)
    with log.open('ab') as output:
        process = subprocess.Popen([str(argument) for argument in arguments], env=environment,
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=0x08000000 if os.name == 'nt' else 0)
        try:
            while process.poll() is None:
                cancelled.wait(0.25)
                check_cancelled()
            check_cancelled()
        finally:
            if process.poll() is None:
                # Only terminate subprocesses owned by this setup invocation.
                try:
                    descendants = psutil.Process(process.pid).children(recursive=True)
                except psutil.NoSuchProcess:
                    descendants = []
                for descendant in reversed(descendants):
                    try:
                        descendant.kill()
                    except psutil.NoSuchProcess:
                        pass
                process.kill()
                process.wait()
    if process.returncode:
        # Package URLs are public; configuration and enrollment never reach pip.
        tail = log.read_bytes()[-2500:].decode('utf-8', errors='replace')
        raise RuntimeError(f'{label} failed. {tail}')


def install(resources, data_directory):
    from filelock import FileLock, Timeout
    setup = resources / 'setup'
    descriptor = json.loads((setup / 'manifest.json').read_text())
    if not descriptor['supported'] or descriptor['target'] != target():
        raise ValueError('This platform uses the bundled base runtime; NVIDIA runtime setup is available on Windows and Linux x64')
    if not re.fullmatch(r'cuda-[0-9a-f]{64}', descriptor['identity']):
        raise ValueError('Invalid GPU runtime identity')
    requirements = setup / descriptor['requirements']
    if requirements.parent != setup or digest(requirements) != descriptor['requirementsSha256']:
        raise ValueError('GPU runtime requirements failed checksum verification')
    if not data_directory.is_absolute() or '..' in data_directory.parts or data_directory.is_symlink():
        raise ValueError('GPU setup requires an absolute, real worker data directory')
    runtimes = data_directory / 'runtimes'
    runtimes.mkdir(parents=True, exist_ok=True)
    if runtimes.is_symlink():
        raise ValueError('GPU runtime directory cannot be a filesystem link')
    if os.name != 'nt':
        runtimes.chmod(0o700)
    destination = runtimes / descriptor['identity']
    try:
        with FileLock(str(runtimes / '.setup.lock'), timeout=0):
            if ready(destination, descriptor, verify=True):
                environment = os.environ.copy()
                for name in ('PYTHONHOME', 'PYTHONPATH', '__PYVENV_LAUNCHER__'):
                    environment.pop(name, None)
                run_command([python_binary(destination), '-I', '-c', 'import torch, pycolmap, cv2, open3d'],
                            destination / 'setup.log', 'Rechecking installed GPU libraries', environment)
                event('GPU runtime is already installed and verified')
                return {'installed': True, 'directory': str(destination), 'identity': descriptor['identity']}
            if shutil.disk_usage(runtimes).free < 25 * 1024 ** 3:
                raise ValueError('GPU runtime setup needs at least 25 GiB of free disk space')
            cache = runtimes / 'cache'
            staging = Path(tempfile.mkdtemp(prefix='.install-', dir=runtimes))
            try:
                extract_python(download(descriptor['python'], cache, 'Downloading the GPU Python runtime'), staging)
                python = python_binary(staging)
                if not python.is_file():
                    raise ValueError('Downloaded Python runtime is incomplete')
                if os.name == 'nt':
                    crt = resources / 'windows-crt'
                    manifest = json.loads((crt / 'manifest.json').read_text())
                    for item in manifest['files']:
                        name = item['fileName']
                        if Path(name).name != name or digest(crt / name) != item['sha256']:
                            raise ValueError('Bundled Microsoft runtime failed checksum verification')
                        shutil.copy2(crt / name, python.parent / name)
                environment = os.environ.copy()
                for name in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONUSERBASE', 'PYTHONSTARTUP', 'PYTHONINSPECT', '__PYVENV_LAUNCHER__'):
                    environment.pop(name, None)
                environment['PYTHONNOUSERSITE'] = '1'
                log = staging / 'setup.log'
                run_command([python, '-I', '-m', 'pip', '--isolated', '--cache-dir', cache / 'pip', 'install',
                             '--disable-pip-version-check', '--require-hashes', '--only-binary=:all:', '--no-compile',
                             '-r', requirements], log, 'Installing GPU libraries (several GB)', environment)
                run_command([python, '-I', '-c', 'import torch, pycolmap, cv2, open3d; print(torch.__version__, pycolmap.__version__)'],
                            log, 'Verifying the installed GPU libraries', environment)
                hub = staging / 'models/hub'
                (hub / 'checkpoints').mkdir(parents=True)
                shutil.copy2(download(descriptor['models']['roma'], cache, 'Downloading reconstruction model weights'), hub / 'checkpoints/romav2.pt')
                source = staging / '.dinov3'
                source.mkdir()
                extracted = extract_dinov3(download(descriptor['models']['dinov3'], cache, 'Downloading pinned model source'), source)
                extracted.rename(hub / ('facebookresearch_dinov3_' + descriptor['models']['dinov3Commit']))
                source.rmdir()
                (hub / 'trusted_list').write_text('')
                shutil.copytree(resources / 'notices', staging / 'notices')
                shutil.copy2(requirements, staging / 'notices/python-requirements.txt')
                manifest = {'version': 1, 'identity': descriptor['identity'], 'target': descriptor['target'],
                            'requirementsSha256': descriptor['requirementsSha256'], 'pythonSha256': digest(python),
                            'models': descriptor['models']}
                with (staging / 'manifest.json').open('w') as output:
                    json.dump(manifest, output, indent=2)
                    output.flush()
                    os.fsync(output.fileno())
                if not ready(staging, descriptor, verify=True):
                    raise ValueError('GPU runtime verification failed before activation')
                check_cancelled()
                # Keep any broken older install recoverable; never touch a scan,
                # checkpoint, profile, or a different runtime identity.
                backup = None
                if destination.exists():
                    backup = runtimes / ('.previous-' + descriptor['identity'] + '-' + uuid.uuid4().hex)
                    destination.rename(backup)
                try:
                    staging.rename(destination)
                except OSError:
                    if backup:
                        backup.rename(destination)
                    raise
                event('GPU runtime installed and verified')
                return {'installed': True, 'directory': str(destination), 'identity': descriptor['identity']}
            finally:
                shutil.rmtree(staging, ignore_errors=True)
    except Timeout as error:
        raise RuntimeError('GPU runtime setup is already running') from error


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resources', type=Path, required=True)
    parser.add_argument('--data-directory', type=Path, required=True)
    parser.add_argument('--parent-pid', type=int, default=os.getppid())
    arguments = parser.parse_args()
    try:
        for name in (signal.SIGINT, signal.SIGTERM):
            signal.signal(name, lambda *_: cancelled.set())
        watch_parent(arguments.parent_pid)
        result = install(arguments.resources.resolve(), arguments.data_directory)
        print(json.dumps({'type': 'runtime-setup-complete', **result}), flush=True)
    except Exception as error:
        print(json.dumps({'type': 'runtime-setup-error', 'message': str(error)}), flush=True)
        sys.exit(1)
