"""Portable engine subprocess for the persistent scan worker.

The worker supplies authorized local paths, restores archives, and persists events.
This module owns engine execution, immutable resume manifests and export validation.
Stdout is JSONL; engine output is retained in files beneath outputPath.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import re
import struct
import subprocess
import sys
import tarfile
import time
import uuid

import pipeline_common as common
import scan_settings
from stage_progress import DESCRIPTIONS, parse_progress
from scan_transfer import gaussian_count

ROOT = Path(__file__).resolve().parent
SPIRULA_VERSION = 'v2026.9.30'
PROTOCOL_STREAM = sys.stdout


def emit(kind, **values):
    print(json.dumps(dict(type=kind, timestamp=time.time(), **values), allow_nan=False), file=PROTOCOL_STREAM, flush=True)


def relocate_densification_config(folder, dataset):
    """Rebind only known native paths, even after an interrupted prior relocation."""
    native_config = folder / 'config.json'
    if not native_config.is_file():
        return
    config = json.loads(native_config.read_text())
    expected = {'scene_root': (folder, folder.name),
                'images_subdir': (dataset / 'images', dataset.name + '/images')}
    changed = False
    for key, (destination, suffix) in expected.items():
        value = config.get(key)
        if not isinstance(value, str) or not value.replace('\\', '/').rstrip('/').endswith('/' + suffix):
            raise ValueError(f'Densification resume has an unexpected {key}')
        if value != str(destination):
            config[key] = str(destination)
            changed = True
    if changed:
        shutil.copy2(native_config, folder / ('config-before-relocation-' + uuid.uuid4().hex + '.json'))
        common.save_json(native_config, config)


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
        paths = set()
        for descriptor in Path(f'/proc/{process.pid}/fd').iterdir():
            try:
                paths.add(descriptor.resolve())
            except OSError:
                continue
        yield paths
    finally:
        try:
            os.killpg(process.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass


@contextlib.contextmanager
def gpu_lock():
    """The lock survives a killed supervisor while its engine descendants live."""
    wait = os.environ.get('GPU_LOCK_WAIT') == '1'
    announced = False

    def wait_for_owner():
        nonlocal announced
        if not announced:
            emit('progress', message='Waiting for another worktree to release the GPU')
            announced = True
        if os.name == 'nt' and os.environ.get('WORKER_PARENT_PID'):
            import psutil
            if not psutil.pid_exists(int(os.environ['WORKER_PARENT_PID'])):
                raise InterruptedError('Native worker exited while waiting for the GPU')
        time.sleep(0.25)

    if os.name == 'nt':
        if os.environ.get('WORKER_MODE') != 'native-development':
            raise RuntimeError('Native GPU execution requires WORKER_MODE=native-development')
        import msvcrt
        from windows_job import WindowsJob
        path = Path(os.environ.get('GPU_LOCK_PATH', str(Path(os.environ.get('WORKER_SCRATCH', Path(os.environ['LOCALAPPDATA']) / 'Guerrilla/scratch')) / '.gpu.lock')))
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
    if worker_parent:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        # Node can die without delivering its graceful shutdown signal. The
        # runner then unwinds run_logged, terminating and reaping engine groups.
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'Cannot bind runner lifetime to worker')
        if os.getppid() != int(worker_parent):
            raise RuntimeError('Worker parent no longer owns this runner')
    path = Path(os.environ.get('GPU_LOCK_PATH', '/scratch/.gpu.lock'))
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


def spz_count(path, cap):
    """Validate the pinned converter's gzip SPZ v1-v3, including CRC and lengths."""
    with gzip.open(path, 'rb') as stream:
        header = stream.read(16)
        if len(header) != 16:
            raise ValueError('SPZ header is truncated')
        magic, version, count, degree, fractional, flags, reserved = struct.unpack('<III4B', header)
        if magic != 0x5053474e or version not in (1, 2, 3) or degree > 3 or reserved or flags & ~1:
            raise ValueError('Unsupported or invalid SPZ header')
        if not 0 < count <= cap or fractional > 24:
            raise ValueError(f'SPZ Gaussian count {count} is outside 1..{cap}')
        stride = (6 if version == 1 else 9) + 3 + (4 if version == 3 else 3) + 1 + 3
        stride += ((degree + 1) ** 2 - 1) * 3
        expected, actual = count * stride, 0
        while block := stream.read(1024 * 1024):
            actual += len(block)
            if actual > expected:
                raise ValueError('SPZ has unexpected trailing attribute data')
        if actual != expected:
            raise ValueError('SPZ attributes are truncated')
    return count


def files_under(root):
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Artifact symlinks are not permitted: {path.name}')
        if any(part in ('.archive-acks', '.worker') for part in path.relative_to(root).parts):
            continue
        if path.is_file() and path.name not in ('platform-state.json', 'artifact-manifest.json'):
            yield path


def artifact_manifest(root):
    return [{'path': p.relative_to(root).as_posix(), 'size': p.stat().st_size,
             'sha256': common.file_hash(p)} for p in files_under(root)]


def checkpoint_order(path):
    name = path.parent.name if path.name == 'state.tar' else path.stem
    numbers = re.findall(r'\d+', name)
    return (int(numbers[-1]) if numbers else -1, path.stat().st_mtime_ns if path.exists() else 0)


def retained_path(root, relative):
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('Retention paths must be relative to output')
    candidate = root / relative
    if candidate.is_symlink() or any(parent.is_symlink() for parent in candidate.parents if parent != root and parent.is_relative_to(root)):
        raise ValueError('Retention paths cannot contain symlinks')
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError('Retention path escapes output')
    return resolved


def excluded_path(relative, excluded):
    return any(relative == prefix or relative.startswith(prefix + '/') for prefix in excluded)


def resumable_spirula_checkpoint(candidate, cap):
    try:
        with candidate.open('rb') as stream:
            if candidate.stat().st_size < 1024 or candidate.stat().st_size % 512:
                return False
            stream.seek(-1024, 2)
            if stream.read() != bytes(1024):
                return False
        with tarfile.open(candidate) as archive:
            members = archive.getmembers()
            metadata = json.load(archive.extractfile('state.json'))
            if not metadata.get('full_resume') or not any(member.name in ('world.means.npy', 'world.opacities.npy') for member in members):
                return False
            if metadata.get('cur_num_splats', cap + 1) > cap:
                raise RuntimeError('Spirula exceeded the requested Gaussian cap')
            return not any(member.offset_data + member.size > candidate.stat().st_size for member in members)
    except (OSError, KeyError, ValueError, tarfile.TarError):
        return False


def training_retention_plan(root, state):
    """Read-only plan; callers archive retained bytes before deleting exclusions."""
    root = root.resolve()
    training = state.get('stages', {}).get('training')
    if not training:
        return dict(version=1, excludePaths=[], latestCheckpoint=None, finalPly=None)
    folder = retained_path(root, training)
    checkpoints = state.get('checkpoints', [])
    latest = max(checkpoints, key=lambda item: checkpoint_order(
        retained_path(root, item['path']) / 'state.tar' if item['path'].endswith('.ckpt')
        else retained_path(root, item['path']))) if checkpoints else None
    if 'training' in state.get('completed', {}):
        backend = state['identity']['backend']
        native = (folder / 'run').glob('step-*.ckpt/state.tar') if backend == 'spirula' else folder.rglob('*.resume')
        for source in sorted(native, key=checkpoint_order, reverse=True):
            if 'retained-checkpoints' in source.relative_to(folder).parts:
                continue
            if latest:
                old = retained_path(root, latest['path'])
                if checkpoint_order(source)[0] <= checkpoint_order(old / 'state.tar' if old.is_dir() or old.suffix == '.ckpt' else old)[0]:
                    break
            candidate = source.parent if backend == 'spirula' else source
            if backend == 'spirula':
                if not resumable_spirula_checkpoint(source, state['identity']['maxCap']) or not (candidate.parent / 'config.json').is_file():
                    continue
            elif not source.stat().st_size:
                continue
            latest = dict(path=candidate.relative_to(root).as_posix(), sha256=common.file_hash(source))
            if backend == 'spirula':
                latest['configSha256'] = common.file_hash(candidate.parent / 'config.json')
            break
    keep = retained_path(root, latest['path']) if latest else None
    if latest:
        source = keep / 'state.tar' if keep.is_dir() else keep
        if not source.is_file() or common.file_hash(source) != latest['sha256']:
            raise ValueError('Latest checkpoint changed before retention')
        if keep.is_dir():
            config = keep.parent / 'config.json'
            if not config.is_file() or (latest.get('configSha256') and common.file_hash(config) != latest['configSha256']):
                raise ValueError('Latest checkpoint configuration changed before retention')
    excluded = set()
    if keep:
        # Snapshot UUID directories contain both the checkpoint and its config.
        for retained in (folder / 'retained-checkpoints', root / 'retained-checkpoints'):
            if retained.exists():
                for previous in retained.iterdir():
                    if not keep.is_relative_to(previous):
                        excluded.add(previous.relative_to(root).as_posix())
        for item in checkpoints:
            previous = retained_path(root, item['path'])
            if previous != keep and 'retained-checkpoints' in previous.relative_to(root).parts:
                excluded.add(previous.parent.relative_to(root).as_posix())
        native = list(folder.rglob('*.ckpt')) + list(folder.rglob('*.resume'))
        # Completed manifests also identify obsolete paths omitted from a clone.
        for item in state.get('completed', {}).get('training', {}).get('files', []):
            relative = Path(item['path'])
            for index, part in enumerate(relative.parts):
                if part.endswith(('.ckpt', '.resume')):
                    native.append(retained_path(root, Path(*relative.parts[:index + 1])))
                    break
        for previous in native:
            if 'retained-checkpoints' not in previous.relative_to(root).parts and previous != keep:
                excluded.add(previous.relative_to(root).as_posix())
    final_ply = None
    candidates = [p for p in (folder / 'final').glob('*.ply') if p.is_file()]
    if not candidates and 'training' in state.get('completed', {}):
        if state['identity']['backend'] == 'spirula':
            source_plys = list((folder / 'run').glob('step-*.ckpt/splat.ply'))
            if source_plys:
                candidates = [max(source_plys, key=lambda p: checkpoint_order(p.parent / 'state.tar'))]
        else:
            try:
                candidates = [common.find_ply(folder)]
            except RuntimeError:
                pass
    if candidates:
        final_ply = candidates[0].relative_to(root).as_posix()
        previous_plys = list(folder.rglob('*.ply')) + [retained_path(root, item['path'])
            for item in state.get('completed', {}).get('training', {}).get('files', []) if item['path'].endswith('.ply')]
        for previous in previous_plys:
            if previous not in candidates and (not keep or not previous.is_relative_to(keep)):
                excluded.add(previous.relative_to(root).as_posix())
    for relative in excluded:
        retained_path(root, relative)
    # A parent directory exclusion already covers its descendants.
    excluded = sorted(p for p in excluded if not any(p.startswith(other + '/') for other in excluded))
    return dict(version=1, excludePaths=excluded, latestCheckpoint=latest, finalPly=final_ply)


def compact_state(root):
    """Normalize a caller-owned clone; never delete bytes or invoke an engine."""
    root = root.resolve()
    state_path = root / 'platform-state.json'
    state = json.loads(state_path.read_text())
    plan = training_retention_plan(root, state)
    # A filtered clone may omit superseded bytes; verify everything retained.
    for stage in state.get('completed', {}).values():
        for item in stage['files']:
            if excluded_path(item['path'], plan['excludePaths']) and item['path'] != plan['finalPly']:
                continue
            path = retained_path(root, item['path'])
            if not path.is_file() or common.file_hash(path) != item['sha256']:
                raise ValueError('Completed artifact changed before compaction')
    if 'training' in state.get('completed', {}):
        folder = retained_path(root, state['completed']['training']['directory'])
        preserve_final_ply(folder, state['identity']['backend'])
    if plan['latestCheckpoint']:
        latest = plan['latestCheckpoint']
        source = retained_path(root, latest['path'])
        folder = retained_path(root, state['stages']['training'])
        if not source.is_relative_to(folder / 'retained-checkpoints'):
            snapshot = folder / 'retained-checkpoints' / uuid.uuid4().hex / source.name
            snapshot.parent.mkdir(parents=True)
            if source.is_dir():
                shutil.copytree(source, snapshot)
                shutil.copy2(source.parent / 'config.json', snapshot.parent / 'config.json')
            else:
                shutil.copy2(source, snapshot)
            latest = dict(latest, path=snapshot.relative_to(root).as_posix())
        state['checkpoints'] = [latest]
    plan = training_retention_plan(root, state)
    state['checkpoints'] = [plan['latestCheckpoint']] if plan['latestCheckpoint'] else []
    state['retention'] = dict(version=1, excludePaths=plan['excludePaths'])
    if 'training' in state.get('completed', {}):
        folder = retained_path(root, state['completed']['training']['directory'])
        state['completed']['training']['files'] = [
            dict(path=p.relative_to(root).as_posix(), sha256=common.file_hash(p), size=p.stat().st_size)
            for p in files_under(folder) if not excluded_path(p.relative_to(root).as_posix(), plan['excludePaths'])]
    common.save_json(state_path, state)
    manifest = dict(version=1, files=[item for item in artifact_manifest(root)
                                   if not excluded_path(item['path'], plan['excludePaths'])])
    common.save_json(root / 'artifact-manifest.json', manifest)
    plan['retainedFiles'] = [item['path'] for item in manifest['files']]
    return plan


def preserve_final_ply(folder, backend):
    if list((folder / 'final').glob('*.ply')):
        return
    if backend == 'spirula':
        candidates = list((folder / 'run').glob('step-*.ckpt/splat.ply'))
        if not candidates:
            return
        source = max(candidates, key=lambda p: checkpoint_order(p.parent / 'state.tar'))
        config = source.parent.parent / 'config.json'
    else:
        try:
            source = common.find_ply(folder)
        except RuntimeError:
            return
        config = folder / 'config.json'
    final = folder / 'final'
    final.mkdir(exist_ok=True)
    shutil.copy2(source, final / source.name)
    if config.is_file():
        shutil.copy2(config, final / 'config.json')


def validate_request(request):
    if request.get('backend') not in ('lichtfeld', 'spirula'):
        raise ValueError('Unknown scan backend')
    cap = request.get('maxCap')
    if type(cap) is not int or cap < 1:
        raise ValueError('maxCap must be a positive integer')
    for name in ('scanId', 'attemptId', 'inputPath', 'outputPath'):
        if not isinstance(request.get(name), str) or not request[name]:
            raise ValueError(f'{name} is required')
    source, output = Path(request['inputPath']).resolve(), Path(request['outputPath']).resolve()
    if not source.is_file() or Path(request['inputPath']).is_symlink():
        raise ValueError('Stored input is not a regular file')
    if source == output or source.is_relative_to(output):
        raise ValueError('Output must not contain the stored input')
    settings = request.get('settings', {})
    if request['backend'] == 'lichtfeld':
        from jsonschema import Draft7Validator
        Draft7Validator(json.loads(scan_settings.SCHEMA_PATH.read_text())).validate(settings)
        if settings.get('training', {}).get('max_cap', cap) != cap:
            raise ValueError('Training max_cap must equal the requested maximum')
    else:
        if set(settings) - {'extraction', 'reconstruction', 'training'}:
            raise ValueError('Unknown Spirula settings section')
        extraction = settings.get('extraction', {})
        bounds = {'skip': (1, 100000), 'keep': (1, 100000), 'max_frames': (1, 100000),
                  'quality': (0, 100), 'scale': (0.01, 1)}
        if set(extraction) - set(bounds) or settings.get('reconstruction', {}):
            raise ValueError('Unsupported Spirula extraction/reconstruction settings')
        for key, value in extraction.items():
            low, high = bounds[key]
            if type(value) not in ((int, float) if key == 'scale' else (int,)) or not low <= value <= high:
                raise ValueError(f'Invalid Spirula {key}')
        training = settings.get('training', {})
        if set(training) - {'iterations'} or type(training.get('iterations', 30000)) is not int or not 1 <= training.get('iterations', 30000) <= 1000000:
            raise ValueError('Invalid Spirula training settings')
    return source, output


def runtime_versions(backend):
    record = ROOT / 'runtime-versions.json'
    versions = json.loads(record.read_text()) if record.exists() else {}
    versions['pipelineSha256'] = common.fingerprint(sorted(ROOT.glob('*.py')) + [scan_settings.SCHEMA_PATH])
    worker_bundle = Path(os.environ.get('WORKER_BUNDLE', '/opt/worker/worker.mjs'))
    if worker_bundle.is_file():
        versions['workerSha256'] = common.file_hash(worker_bundle)
    studio = common.studio_path()
    if not studio.is_file():
        raise RuntimeError('LichtFeld converter is unavailable')
    versions['lichtfeld'] = subprocess.check_output([str(studio), '--version'], text=True, stderr=subprocess.STDOUT).strip()
    if os.name == 'nt':
        versions['mode'] = 'native-development'
        for name, plugin in (('colmapPlugin', common.plugin_path()), ('densificationPlugin', common.densification_plugin_path())):
            sources = sorted(path for path in plugin.rglob('*.py') if '.venv' not in path.parts and '__pycache__' not in path.parts)
            if sources:
                versions[name] = common.fingerprint(sources)
    if backend == 'spirula':
        executable = Path(os.environ.get('SPIRULA_BIN', '/opt/spirula/spirula'))
        if not executable.is_file():
            raise RuntimeError('Spirula executable is unavailable')
        versions['spirula'] = SPIRULA_VERSION
        versions['spirulaBinarySha256'] = common.file_hash(executable)
    return versions


def preflight():
    """Report prerequisites honestly; only real scene validation proves a workflow."""
    result = {'runtimeVersions': {}, 'backends': {}}
    for backend in ('lichtfeld', 'spirula'):
        try:
            result['runtimeVersions'].update(runtime_versions(backend))
            if backend == 'lichtfeld':
                if os.name == 'nt':
                    studio_python = common.studio_path().parent / 'python.exe'
                    probe = ('import sys,site; from pathlib import Path; '
                             'p=Path(sys.argv[1]); site.addsitedir(str(p/".venv/Lib/site-packages")); '
                             'import torch,pycolmap,lichtfeld; '
                             'assert torch.cuda.is_available(); assert pycolmap.has_cuda; '
                             'assert hasattr(pycolmap,"global_mapping")')
                    subprocess.run([str(studio_python), '-c', probe, str(common.densification_plugin_path())],
                                   capture_output=True, text=True, check=True, timeout=60)
                    result['backends'][backend] = {'available': True, 'workflowVerified': False}
                    continue
                import pycolmap
                import torch
                if not pycolmap.has_cuda or not torch.cuda.is_available():
                    raise RuntimeError('CUDA reconstruction/training is unavailable')
                for plugin, filename in ((common.plugin_path(), 'panels/main_panel.py'),
                                         (common.densification_plugin_path(), 'densify.py')):
                    if not (plugin / filename).is_file():
                        raise RuntimeError(f'Missing Linux plugin: {plugin.name}')
                import lichtfeld  # noqa: F401 — native plugin dependency must load
                import importlib
                sys.path.insert(0, str(common.plugin_path().parent))
                importlib.import_module(common.plugin_path().name + '.panels.main_panel')
                importlib.import_module(common.densification_plugin_path().name + '.densify')
            else:
                if os.name == 'nt':
                    executable = os.environ.get('SPIRULA_BIN', '')
                    absent = Path(os.environ.get('TEMP', '.')) / ('guerrilla-preflight-' + uuid.uuid4().hex)
                    proc = subprocess.run([executable, 'train', '--device', 'NVIDIA', '--data', str(absent),
                                           '--cap-max', '1', '--num-iterations', '1', '--disable-viewer', '1', '--keep-viewer-alive', '0'],
                                          text=True, capture_output=True, timeout=30)
                    report = proc.stdout + proc.stderr
                    if 'NVIDIA' not in report or 'dataset path does not exist' not in report:
                        raise RuntimeError('Native Spirula NVIDIA Vulkan preflight failed: ' + report[-1200:])
                    result['backends'][backend] = {'available': True, 'workflowVerified': False}
                    continue
                proc = subprocess.run(['vulkaninfo', '--summary'], text=True, capture_output=True, timeout=30)
                report = proc.stdout + proc.stderr
                if proc.returncode or not re.search(r'deviceType\s*=\s*PHYSICAL_DEVICE_TYPE_(DISCRETE|INTEGRATED)_GPU', report):
                    raise RuntimeError('No hardware Vulkan GPU is available inside Docker; CPU renderers are rejected')
            result['backends'][backend] = {'available': True, 'workflowVerified': False}
        except Exception as exc:
            result['backends'][backend] = {'available': False, 'workflowVerified': False, 'error': str(exc)[:2000]}
    return result


class Runner:
    def __init__(self, request):
        self.request = request
        self.source, self.output = validate_request(request)
        self.cap = request['maxCap']
        self.backend = request['backend']
        self.output.mkdir(parents=True, exist_ok=True)
        self.state_path = self.output / 'platform-state.json'
        versions = runtime_versions(self.backend)
        if request.get('runtimeVersions') and request['runtimeVersions'] != versions:
            raise ValueError('The recorded engine runtime is unavailable; resume cannot change versions')
        identity = dict(backend=self.backend, settings=request['settings'], maxCap=self.cap,
                        inputSha256=common.file_hash(self.source), runtimeVersions=versions)
        if self.state_path.exists():
            if not request.get('resume'):
                raise ValueError('An existing job requires resume')
            self.state = json.loads(self.state_path.read_text())
            if self.state['identity'] != identity:
                raise ValueError('Resume input, settings, cap or engine versions changed')
            for stage in self.state['completed'].values():
                for item in stage['files']:
                    path = self.path(item['path'])
                    if not path.is_file() or common.file_hash(path) != item['sha256']:
                        raise ValueError(f'Completed artifact changed: {item["path"]}')
            for item in self.state.get('checkpoints', []):
                checkpoint = self.path(item['path'])
                source = checkpoint / 'state.tar' if checkpoint.is_dir() else checkpoint
                if not source.is_file() or common.file_hash(source) != item['sha256']:
                    raise ValueError('Archived native checkpoint changed')
                if 'configSha256' in item:
                    config = checkpoint.parent / 'config.json'
                    if not config.is_file() or common.file_hash(config) != item['configSha256']:
                        raise ValueError('Archived native checkpoint configuration changed')
        else:
            self.state = dict(version=1, identity=identity, completed={}, stages={})
        self.previous_output = self.state.get('scratchPath', str(self.output))
        self.state['scratchPath'] = str(self.output)
        self.save()
        self.last_checkpoint_poll = 0
        self.checkpoint_signatures = {}
        emit('runtime', runtimeVersions=versions)

    def path(self, relative):
        path = (self.output / relative).resolve()
        if not path.is_relative_to(self.output) or path == self.output:
            raise ValueError('Artifact path escapes the job')
        return path

    def save(self):
        common.save_json(self.state_path, self.state)

    def checkpoint(self, stage):
        if not self.request.get('archiveAck'):
            return
        checkpoint_id = uuid.uuid4().hex
        emit('checkpoint', stage=stage, checkpointId=checkpoint_id,
             manifestPath='platform-state.json', **self.state.get('result', {}))
        ack = self.output / '.archive-acks' / checkpoint_id
        deadline = time.monotonic() + self.request.get('archiveTimeoutSeconds', 3600)
        while not ack.is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError('Artifact archival was not acknowledged; scratch has been retained')
            time.sleep(0.25)

    def command(self, args, log):
        # Keep common helper's diagnostics out of the JSONL protocol.
        self.active_log = log
        with contextlib.redirect_stdout(sys.stderr):
            common.run_logged(args, log, on_tick=self.poll_checkpoints, on_progress=self.report_log)

    def report_log(self, log, force=False):
        now = time.monotonic()
        if not force and now - self.last_progress_poll < 2:
            return
        self.last_progress_poll = now
        logs = [log]
        if self.state['stage'] == 'reconstruction':
            logs.append(self.path(self.state['stages']['reconstruction']) / 'colmap.log')
        for source in logs:
            if not source.is_file():
                continue
            size = source.stat().st_size
            offset = self.progress_offsets.get(str(source), 0)
            if size < offset:
                offset = 0
            with source.open('rb') as stream:
                stream.seek(max(offset, size - 65536))
                text = stream.read().decode('utf-8', errors='replace')
                self.progress_offsets[str(source)] = stream.tell()
            text = self.progress_fragments.get(str(source), '') + text
            boundary = max(text.rfind('\n'), text.rfind('\r')) + 1
            self.progress_fragments[str(source)] = '' if force else text[boundary:][-65536:]
            if not force:
                text = text[:boundary]
            update = parse_progress(self.state['stage'], text)
            if update:
                if update.get('substep') != self.progress_values.get('substep'):
                    self.progress_values = {}
                    self.progress_baseline = None
                self.progress_values.update(update)
                self.progress_values['measuredAt'] = time.time()
        values = dict(self.progress_values)
        current, total = values.get('current'), values.get('total')
        if current is not None and total and values.get('unit') != 'percent':
            if self.progress_baseline is None or current < self.progress_baseline[1]:
                self.progress_baseline = (now, current)
            elapsed, advanced = now - self.progress_baseline[0], current - self.progress_baseline[1]
            if elapsed >= 2 and advanced > 0:
                values['rate'] = advanced / elapsed
                if 'remainingSeconds' not in values:
                    values['remainingSeconds'] = (total - current) / values['rate']
        emit('progress', stage=self.state['stage'], elapsedSeconds=now - self.stage_started,
             **values)

    def poll_checkpoints(self, process):
        if self.state.get('stage') != 'training' or (process is not None and time.monotonic() - self.last_checkpoint_poll < 10):
            return
        self.last_checkpoint_poll = time.monotonic()
        folder = self.path(self.state['stages']['training'])
        candidates = list(folder.rglob('state.tar')) if self.backend == 'spirula' else list(folder.rglob('*.resume'))
        candidates = [p for p in candidates if 'retained-checkpoints' not in p.parts]
        candidates = [p for p in candidates if self.checkpoint_signatures.get(str(p)) != (p.stat().st_size, p.stat().st_mtime_ns)]
        candidates.sort(key=checkpoint_order, reverse=True)
        if not candidates or (process is not None and process.poll() is not None):
            return
        # Freeze every engine thread before snapshotting. Open native checkpoint
        # files are never accepted, even if their current size looks plausible.
        with (paused_engine(process) if process is not None else contextlib.nullcontext(set())) as open_paths:
            changed = False
            snapshots = self.state.setdefault('checkpoints', [])
            hashes = {item['sha256'] for item in snapshots}
            for candidate in candidates:
                if snapshots:
                    previous = self.path(snapshots[-1]['path'])
                    previous_source = previous / 'state.tar' if previous.is_dir() else previous
                    if checkpoint_order(candidate)[0] < checkpoint_order(previous_source)[0]:
                        continue
                if candidate.resolve() in open_paths or candidate.stat().st_size == 0:
                    continue
                identity = common.file_hash(candidate)
                if identity in hashes:
                    self.checkpoint_signatures[str(candidate)] = (candidate.stat().st_size, candidate.stat().st_mtime_ns)
                    continue
                if self.backend == 'spirula':
                    # Spirula may rewrite a checkpoint at the same step after
                    # resume. Keep every member in a runner-owned snapshot, and
                    # defer copying while any native member remains open.
                    native_directory = candidate.parent.resolve()
                    native_config = candidate.parent.parent / 'config.json'
                    if native_config.is_symlink():
                        raise ValueError('Native checkpoint configuration is a symlink')
                    if not native_config.is_file():
                        continue
                    if native_config.resolve() in open_paths or any(path == native_directory or path.is_relative_to(native_directory)
                           for path in open_paths):
                        continue
                    if any(path.is_symlink() for path in candidate.parent.rglob('*')):
                        raise ValueError('Native checkpoint contains a symlink')
                    # state.tar is written last. Validate its member bounds and
                    # resumable metadata before recording a recovery checkpoint.
                    if not resumable_spirula_checkpoint(candidate, self.cap):
                        continue
                    snapshot = folder / 'retained-checkpoints' / uuid.uuid4().hex / candidate.parent.name
                    snapshot.parent.mkdir(parents=True)
                    shutil.copytree(candidate.parent, snapshot)
                    # Spirula resolves the run configuration from the checkpoint's
                    # parent, so retain it beside the copied directory verbatim.
                    shutil.copy2(native_config, snapshot.parent / 'config.json')
                else:
                    snapshot = folder / 'retained-checkpoints' / uuid.uuid4().hex / candidate.name
                    snapshot.parent.mkdir(parents=True)
                    shutil.copy2(candidate, snapshot)
                recorded = {'path': snapshot.relative_to(self.output).as_posix(), 'sha256': identity}
                if self.backend == 'spirula':
                    recorded['configSha256'] = common.file_hash(snapshot.parent / 'config.json')
                self.state['checkpoints'] = [recorded]
                hashes.add(identity)
                self.checkpoint_signatures[str(candidate)] = (candidate.stat().st_size, candidate.stat().st_mtime_ns)
                changed = True
                break
            if changed:
                plan = training_retention_plan(self.output, self.state)
                self.state['retention'] = dict(version=1, excludePaths=plan['excludePaths'])
                self.save()
                self.checkpoint('training')
                if self.request.get('archiveAck'):
                    self.prune_training(plan, open_paths, candidate if process is not None else None)

    def prune_training(self, plan, open_paths=(), active_checkpoint=None):
        """Called only after the replacement checkpoint's archive is acknowledged."""
        latest = self.path(plan['latestCheckpoint']['path']) if plan.get('latestCheckpoint') else None
        latest_source = latest / 'state.tar' if latest and latest.is_dir() else latest
        for relative in plan['excludePaths']:
            path = retained_path(self.output, relative)
            if not path.exists():
                continue
            if any(opened == path or opened.is_relative_to(path) for opened in open_paths):
                continue
            if active_checkpoint is not None:
                if active_checkpoint == path or active_checkpoint.is_relative_to(path):
                    continue
                # An unfinished newer native checkpoint is not superseded yet.
                source = path / 'state.tar' if path.suffix == '.ckpt' else path
                if path.suffix in ('.ckpt', '.resume') and 'retained-checkpoints' not in path.parts:
                    if not source.is_file() or checkpoint_order(source)[0] > checkpoint_order(latest_source)[0]:
                        continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    def stage(self, name, function, resumable=False):
        if name in self.state['completed']:
            emit('stage', stage=name, status='completed', resumed=True)
            return self.path(self.state['completed'][name]['directory'])
        previous = self.state['stages'].get(name)
        directory = self.path(previous) if previous and resumable else self.output / f'{name}-{uuid.uuid4().hex[:12]}'
        self.state['stages'][name] = directory.name
        self.state['stage'] = name
        self.save()
        started = self.stage_started = time.monotonic()
        self.last_progress_poll = 0
        self.progress_offsets = {}
        self.progress_fragments = {}
        self.progress_baseline = None
        self.progress_values = dict(substep=DESCRIPTIONS[name])
        emit('stage', stage=name, status='running', elapsedSeconds=0, **self.progress_values)
        function(directory)
        if name == 'training':
            preserve_final_ply(directory, self.backend)
            self.poll_checkpoints(None)
            plan = training_retention_plan(self.output, self.state)
            self.state['retention'] = dict(version=1, excludePaths=plan['excludePaths'])
        files = [{'path': p.relative_to(self.output).as_posix(), 'sha256': common.file_hash(p),
                  'size': p.stat().st_size} for p in files_under(directory)
                 if not excluded_path(p.relative_to(self.output).as_posix(), self.state.get('retention', {}).get('excludePaths', []))]
        if not files:
            raise RuntimeError(f'{name} produced no artifacts')
        self.state['completed'][name] = dict(directory=directory.name, files=files)
        self.save()
        self.checkpoint(name)
        if name == 'training' and self.request.get('archiveAck'):
            self.prune_training(plan)
        emit('stage', stage=name, status='completed', elapsedSeconds=time.monotonic() - started)
        return directory

    def lichtfeld(self):
        options_file = self.output / 'settings.json'
        common.save_json(options_file, self.request['settings'])
        options = scan_settings.load(options_file)
        selected = self.stage('selection', lambda folder: self.command(
            [sys.executable, ROOT / 'select_frames.py', self.source, folder,
             *scan_settings.selection_arguments(options)], self.output / f'{folder.name}.log'))

        def reconstruct(folder):
            self.command([sys.executable, ROOT / 'reconstruct_splat.py', '--images', selected,
                          '--output', folder, '--settings', options_file, '--reconstruction-only',
                          '--retain-artifacts', '--max-cap', self.cap], self.output / f'{folder.name}.log')
        reconstructed = self.stage('reconstruction', reconstruct)
        # Dataset paths in the desktop manifest are not used here: scratch is relocatable.
        dataset = reconstructed / 'dataset'

        def densify(folder):
            # Densification publishes into a copy, keeping reconstruction immutable.
            staged_dataset = folder.parent / (folder.name + '-dataset')
            if not staged_dataset.exists():
                shutil.copytree(dataset, staged_dataset)
            relocate_densification_config(folder, staged_dataset)
            with contextlib.redirect_stdout(sys.stderr):
                common.densify(common.studio_path(), staged_dataset, folder, self.cap,
                               settings=scan_settings.densification(options), on_progress=self.report_log)
        dense = self.stage('densification', densify, resumable=True)
        train_dataset = dense.parent / (dense.name + '-dataset')
        training = self.stage('training', lambda folder: self.train_lichtfeld(folder, train_dataset, options), resumable=True)
        return common.find_ply(training / 'final' if (training / 'final').exists() else training)

    def train_lichtfeld(self, folder, dataset, options):
        self.active_log = folder / 'training.log'
        config = scan_settings.training(options, self.cap)
        checkpoints = [self.path(item['path']) for item in self.state.get('checkpoints', [])
                       if item['path'].endswith('.resume')]
        if not folder.exists():
            with contextlib.redirect_stdout(sys.stderr):
                common.train(common.studio_path(), dataset, folder, config, on_tick=self.poll_checkpoints, on_progress=self.report_log)
            return
        if not checkpoints:
            # Preserve failed work; a new directory is a distinct training attempt.
            archived = folder.with_name(folder.name + '-failed-' + uuid.uuid4().hex[:8])
            folder.rename(archived)
            with contextlib.redirect_stdout(sys.stderr):
                common.train(common.studio_path(), dataset, folder, config, on_tick=self.poll_checkpoints, on_progress=self.report_log)
            return
        self.command([common.studio_path(), '--headless', '--train', '--resume', checkpoints[-1],
                      '-d', dataset, '-o', folder, '--max-cap', self.cap], folder / 'resume.log')
        ply = common.find_ply(folder)
        expected = config['iterations'] + (config['sparsify_steps'] if config['enable_sparsity'] else 0)
        if ply.name != f'splat_{expected}.ply':
            raise RuntimeError('Resumed LichtFeld did not finish the configured training duration')

    def spirula(self):
        executable = Path(os.environ.get('SPIRULA_BIN', '/opt/spirula/spirula'))
        extraction = {'skip': 10, 'keep': 5, 'max_frames': 300, 'quality': 95, 'scale': 1,
                      **self.request['settings'].get('extraction', {})}
        flags = [part for key, value in extraction.items() for part in ('--' + key.replace('_', '-'), str(value))]
        def extract(folder):
            self.command([executable, 'sam', 'extract', self.source, '--out', folder, *flags], self.output / f'{folder.name}.log')
            if len([p for p in folder.rglob('*') if p.suffix.lower() in common.IMAGE_SUFFIXES]) < 3:
                raise RuntimeError('Spirula extraction produced fewer than three images')
        selected = self.stage('selection', extract)
        def reconstruct(folder):
            self.command([executable, 'sfm', 'auto', selected, '-o', folder,
                          '--progress-dir', folder / 'progress', '--no-masks'], self.output / f'{folder.name}.log')
            if not list(folder.rglob('cameras.bin')) or not list(folder.rglob('points3D.bin')):
                raise RuntimeError('Spirula did not produce a COLMAP reconstruction')
            # sfm auto writes sparse/features/matches, not a copy of its images.
            shutil.copytree(selected, folder / 'images', dirs_exist_ok=True)
        dataset = self.stage('reconstruction', reconstruct, resumable=True)
        def train(folder):
            folder.mkdir(parents=True, exist_ok=True)
            checkpoints = [self.path(item['path']) for item in self.state.get('checkpoints', [])
                           if item['path'].endswith('.ckpt')]
            if not checkpoints and (folder / 'run').exists():
                (folder / 'run').rename(folder / ('failed-run-' + uuid.uuid4().hex[:12]))
            command = [executable, 'train', '--data', dataset, '--data-format', 'colmap',
                       '--output-dir-prefix', folder, '--output-dir-name', 'run',
                       '--cap-max', self.cap, '--num-iterations', self.request['settings'].get('training', {}).get('iterations', 30000),
                       '--save-full-checkpoint', '1', '--save-only-latest-checkpoint', '0',
                       '--disable-viewer', '1', '--keep-viewer-alive', '0']
            if checkpoints:
                command += ['--resume', checkpoints[-1]]
            self.command(command, self.output / f'{folder.name}.log')
        training = self.stage('training', train, resumable=True)
        candidates = list((training / 'final').glob('*.ply')) or sorted((training / 'run').glob('step-*.ckpt/splat.ply'), key=lambda p: checkpoint_order(p.parent / 'state.tar'))
        if not candidates:
            raise RuntimeError('Spirula training produced no splat.ply')
        return candidates[-1]

    def run(self):
        ply = self.lichtfeld() if self.backend == 'lichtfeld' else self.spirula()
        expected = gaussian_count(ply, self.cap)
        def export(folder):
            folder.mkdir()
            with contextlib.redirect_stdout(sys.stderr):
                self.progress_values = dict(substep='Creating SOG export', gaussians=expected)
                common.export_sog(common.studio_path(), ply, folder / 'result.sog', folder / 'sog.log', self.cap, on_progress=self.report_log)
                self.progress_values = dict(substep='Creating SPZ export', gaussians=expected)
                common.export_spz(common.studio_path(), ply, folder / 'result.spz', folder / 'spz.log', on_progress=self.report_log)
            if spz_count(folder / 'result.spz', self.cap) != expected:
                raise ValueError('SPZ Gaussian count changed during conversion')
        exports = self.stage('export', export)
        count = gaussian_count(exports / 'result.sog', self.cap)
        if count != expected or spz_count(exports / 'result.spz', self.cap) != count:
            raise ValueError('Export Gaussian counts disagree')
        self.state['result'] = dict(sog=(exports / 'result.sog').relative_to(self.output).as_posix(),
                                    spz=(exports / 'result.spz').relative_to(self.output).as_posix(), gaussians=count)
        self.state['status'] = 'completed'
        self.save()
        common.save_json(self.output / 'artifact-manifest.json', dict(version=1, files=[
            item for item in artifact_manifest(self.output)
            if not excluded_path(item['path'], self.state.get('retention', {}).get('excludePaths', []))]))
        self.checkpoint('finalizing')
        emit('completed', **self.state['result'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path)
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--download')
    parser.add_argument('--public-source', action='store_true')
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--retention-plan', type=Path, help='Print a read-only checkpoint retention plan')
    parser.add_argument('--compact-state', type=Path, help='Normalize manifests in an offline cloned attempt; does not delete files')
    parser.add_argument('--runtime-versions', choices=('spirula', 'lichtfeld'))
    args = parser.parse_args(argv)
    if args.runtime_versions:
        with contextlib.redirect_stdout(sys.stderr):
            versions = runtime_versions(args.runtime_versions)
        print(json.dumps(versions), flush=True)
        return 0
    if args.retention_plan or args.compact_state:
        root = (args.retention_plan or args.compact_state).resolve()
        plan = compact_state(root) if args.compact_state else training_retention_plan(root, json.loads((root / 'platform-state.json').read_text()))
        print(json.dumps(plan), flush=True)
        return 0
    if args.preflight:
        with contextlib.redirect_stdout(sys.stderr):
            report = preflight()
        print(json.dumps(report), flush=True)
        return 0
    if args.download:
        if not args.destination:
            parser.error('--destination is required for --download')
        if args.public_source:
            from public_download import download_public_video as download_video
        else:
            from scan_transfer import download_video
        args.destination.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.redirect_stdout(sys.stderr):
            download_video(args.download, args.destination)
        emit('downloaded', path=str(args.destination), sha256=common.file_hash(args.destination), size=args.destination.stat().st_size)
        return 0
    if not args.request:
        parser.error('--request is required')
    request = json.loads(args.request.read_text(encoding='utf-8'))
    def interrupted(signum, _frame):
        raise InterruptedError(f'Runner interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        with gpu_lock():
            with contextlib.redirect_stdout(sys.stderr):
                report = preflight()
            capability = report['backends'].get(request.get('backend'), {})
            if not capability.get('available'):
                raise RuntimeError(capability.get('error', 'Unknown backend'))
            Runner(request).run()
        return 0
    except BaseException as exc:
        emit('failed', error=str(exc)[:2000], interrupted=isinstance(exc, (KeyboardInterrupt, InterruptedError)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
