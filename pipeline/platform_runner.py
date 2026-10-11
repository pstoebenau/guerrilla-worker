"""Portable engine subprocess for the persistent scan worker.

The worker supplies authorized local paths, restores archives, and persists events.
This module owns engine execution, immutable resume manifests and export validation.
Stdout is JSONL; engine output is retained in files beneath outputPath.
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import os
from pathlib import Path
import shutil
import signal
import re
import struct
import sys
import time
import uuid

# Studio's embedded Python omits the script directory from sys.path.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pipeline_common as common
import scan_settings
from stage_progress import DESCRIPTIONS, parse_progress
from scan_transfer import gaussian_count
from engine import Engine
from engines import ENGINES, get_engine
from process_runtime import paused_engine
from process_runtime import gpu_lock as _gpu_lock

PROTOCOL_STREAM = sys.stdout


def emit(kind, **values):
    print(json.dumps(dict(type=kind, timestamp=time.time(), **values), allow_nan=False), file=PROTOCOL_STREAM, flush=True)


def gpu_lock():
    return _gpu_lock(lambda: emit('progress', message='Waiting for another worktree to release the GPU'))


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


def training_retention_plan(root, state, *, engine=None):
    """Read-only plan; callers archive retained bytes before deleting exclusions."""
    root = root.resolve()
    training = state.get('stages', {}).get('training')
    if not training:
        return dict(version=1, excludePaths=[], latestCheckpoint=None, finalPly=None)
    engine = engine if engine is not None else get_engine(state['identity']['backend'])
    folder = retained_path(root, training)
    checkpoints = state.get('checkpoints', [])
    latest = max(checkpoints, key=lambda item: engine.checkpoint(retained_path(root, item['path'])).order) if checkpoints else None
    if 'training' in state.get('completed', {}):
        native = engine.checkpoint_candidates(folder)
        for candidate in sorted(native, key=lambda item: item.order, reverse=True):
            if 'retained-checkpoints' in candidate.path.relative_to(folder).parts:
                continue
            if latest:
                old = engine.checkpoint(retained_path(root, latest['path']))
                if candidate.step <= old.step:
                    break
            if not engine.valid_checkpoint(candidate, state['identity']['maxCap']):
                continue
            if candidate.config is not None and not candidate.config.is_file():
                continue
            latest = dict(path=candidate.path.relative_to(root).as_posix(), sha256=common.file_hash(candidate.source))
            if candidate.config is not None:
                latest['configSha256'] = common.file_hash(candidate.config)
            break
    keep = retained_path(root, latest['path']) if latest else None
    if latest:
        checkpoint = engine.checkpoint(keep)
        if not checkpoint.source.is_file() or common.file_hash(checkpoint.source) != latest['sha256']:
            raise ValueError('Latest checkpoint changed before retention')
        if checkpoint.config is not None:
            config = checkpoint.config
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
        native = list(folder.rglob('*' + engine.checkpoint_suffix))
        # Completed manifests also identify obsolete paths omitted from a clone.
        for item in state.get('completed', {}).get('training', {}).get('files', []):
            relative = Path(item['path'])
            for index, part in enumerate(relative.parts):
                if part.endswith(engine.checkpoint_suffix):
                    native.append(retained_path(root, Path(*relative.parts[:index + 1])))
                    break
        for previous in native:
            if 'retained-checkpoints' not in previous.relative_to(root).parts and previous != keep:
                excluded.add(previous.relative_to(root).as_posix())
    final_ply = None
    candidates = [p for p in (folder / 'final').glob('*.ply') if p.is_file()]
    if not candidates and 'training' in state.get('completed', {}):
        try:
            candidates = [engine.training_output(folder).ply]
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
    engine = get_engine(state['identity']['backend'])
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
            else:
                shutil.copy2(source, snapshot)
            native_config = engine.checkpoint(source).config
            if native_config is not None:
                shutil.copy2(native_config, engine.checkpoint(snapshot).config)
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


def preserve_final_ply(folder, backend, *, engine=None):
    if list((folder / 'final').glob('*.ply')):
        return
    engine = engine if engine is not None else get_engine(backend)
    try:
        result = engine.training_output(folder)
    except RuntimeError:
        return
    final = folder / 'final'
    final.mkdir(exist_ok=True)
    shutil.copy2(result.ply, final / result.ply.name)
    if result.config is not None and result.config.is_file():
        shutil.copy2(result.config, final / 'config.json')


def validate_request(request, *, engine=None):
    engine = engine if engine is not None else get_engine(request.get('backend'))
    if engine.name != request.get('backend'):
        raise ValueError('Engine does not match the requested backend')
    cap = request.get('maxCap')
    if type(cap) is not int or cap < 1:
        raise ValueError('maxCap must be a positive integer')
    for name in ('scanId', 'attemptId', 'inputPath', 'outputPath'):
        if not isinstance(request.get(name), str) or not request[name]:
            raise ValueError(f'{name} is required')
    source, output = Path(request['inputPath']).resolve(), Path(request['outputPath']).resolve()
    selective = request.get('selectiveResume') and request.get('resume') and re.fullmatch(r'[a-f0-9]{64}', request.get('inputSha256') or '')
    if (not source.is_file() and not selective) or Path(request['inputPath']).is_symlink():
        raise ValueError('Stored input is not a regular file')
    if source == output or source.is_relative_to(output):
        raise ValueError('Output must not contain the stored input')
    settings = request.get('settings', {})
    if not isinstance(settings, dict) or any(not isinstance(section, dict) for section in settings.values()):
        raise ValueError('Settings must contain objects for each section')
    engine.validate_settings(settings, cap)
    return source, output


def runtime_versions(backend, *, engine=None):
    engine = engine if engine is not None else get_engine(backend)
    record = ROOT / 'runtime-versions.json'
    versions = engine.runtime_versions(json.loads(record.read_text()) if record.exists() else {})
    versions['pipelineSha256'] = common.fingerprint(sorted(ROOT.glob('*.py')) + [scan_settings.SCHEMA_PATH])
    worker_bundle = Path(os.environ.get('WORKER_BUNDLE', '/opt/worker/worker.mjs'))
    if worker_bundle.is_file():
        versions['workerSha256'] = common.file_hash(worker_bundle)
    versions['splatTransform'] = common.converter_version()
    versions['splatTransformGpuBackend'] = common.converter_gpu_backend()
    versions['splatTransformGpu'] = os.environ.get('SPLAT_TRANSFORM_GPU', 'auto')
    return versions


def preflight(engines=None):
    """Report prerequisites honestly; only real scene validation proves a workflow."""
    engines = tuple(ENGINES.values() if engines is None else engines)
    result = {'runtimeVersions': {}, 'backends': {}}
    for engine in engines:
        try:
            versions = runtime_versions(engine.name, engine=engine)
            result['runtimeVersions'].update(versions)
            gpus = engine.preflight()
            if gpus is not None:
                result['gpus'] = gpus
            result['backends'][engine.name] = {'available': True, 'workflowVerified': False, 'runtimeVersions': versions}
        except Exception as exc:
            result['backends'][engine.name] = {'available': False, 'workflowVerified': False, 'error': str(exc)[:2000]}
    cuda = [engine.name for engine in engines if engine.requires_cuda and result['backends'][engine.name]['available']]
    if cuda or any('NVIDIA' in gpu['name'].upper() for gpu in result.get('gpus', [])):
        from gpu_runtime import nvidia_devices
        try:
            result['gpus'] = nvidia_devices()
        except Exception as exc:
            for name in cuda:
                result['backends'][name].update(available=False, error=str(exc)[:2000])
    return result


class Runner:
    def __init__(self, request, *, engine: Engine | None = None):
        self.request = request
        self.engine = engine if engine is not None else get_engine(request.get('backend'))
        self.source, self.output = validate_request(request, engine=self.engine)
        self.cap = request['maxCap']
        self.backend = request['backend']
        self.output.mkdir(parents=True, exist_ok=True)
        self.state_path = self.output / 'platform-state.json'
        versions = runtime_versions(self.backend, engine=self.engine)
        if request.get('runtimeVersions') and request['runtimeVersions'] != versions:
            raise ValueError('The recorded engine runtime is unavailable; resume cannot change versions')
        identity = dict(backend=self.backend, settings=request['settings'], maxCap=self.cap,
                        inputSha256=(request['inputSha256'] if request.get('selectiveResume') else common.file_hash(self.source)), runtimeVersions=versions)
        if self.state_path.exists():
            if not request.get('resume'):
                raise ValueError('An existing job requires resume')
            self.state = json.loads(self.state_path.read_text())
            previous_identity = self.state['identity']
            portable = request.get('selectiveResume') and not self.state.get('stageCheckpoints')
            compared = lambda value: {key: item for key, item in value.items() if not (portable and key == 'runtimeVersions')}
            if compared(previous_identity) != compared(identity):
                raise ValueError('Resume input, settings, cap or engine versions changed')
            self.state['identity'] = identity
            for stage in ([] if request.get('selectiveResume') else self.state['completed'].values()):
                for item in stage['files']:
                    path = self.path(item['path'])
                    if not path.is_file() or common.file_hash(path) != item['sha256']:
                        raise ValueError(f'Completed artifact changed: {item["path"]}')
            for item in ([] if request.get('selectiveResume') else self.state.get('checkpoints', [])):
                checkpoint = self.path(item['path'])
                source = self.engine.checkpoint(checkpoint).source
                if not source.is_file() or common.file_hash(source) != item['sha256']:
                    raise ValueError('Archived native checkpoint changed')
                if 'configSha256' in item:
                    config = self.engine.checkpoint(checkpoint).config
                    if config is None or not config.is_file() or common.file_hash(config) != item['configSha256']:
                        raise ValueError('Archived native checkpoint configuration changed')
        else:
            self.state = dict(version=1, identity=identity, completed={}, stages={})
        self.previous_output = self.state.get('scratchPath', str(self.output))
        self.state['scratchPath'] = str(self.output)
        self.save()
        self.last_checkpoint_poll = 0
        self.checkpoint_signatures = {}
        self.dense_checkpoint_signature = None
        emit('runtime', runtimeVersions=versions)

    def path(self, relative, *, root=None):
        if Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise ValueError('Artifact path escapes the job')
        return retained_path((root if root is not None else self.output).resolve(), relative)

    def save(self):
        common.save_json(self.state_path, self.state)

    def restore(self, *paths):
        """Fetch only dependencies of the stage about to run, through the worker."""
        if not self.request.get('selectiveResume'):
            return
        names = [p.relative_to(self.output).as_posix() for p in paths]
        for name in names:
            self.path(name)
        restore_id = uuid.uuid4().hex
        emit('restore', stage=self.state.get('stage'), restoreId=restore_id, paths=names)
        ack = self.output / '.worker/restored' / restore_id
        deadline = time.monotonic() + self.request.get('archiveTimeoutSeconds', 3600)
        while not ack.is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError('Stage input restoration was not acknowledged')
            time.sleep(0.1)
        for entry in [*self.state['completed'].values(), *self.state.get('stageCheckpoints', {}).values()]:
            for item in entry['files']:
                if any(item['path'] == name or item['path'].startswith(name + '/') for name in names):
                    file = self.path(item['path'])
                    if not file.is_file() or common.file_hash(file) != item['sha256']:
                        raise ValueError(f'Restored stage input changed: {item["path"]}')

    def file_manifest(self, paths):
        return [dict(path=p.relative_to(self.output).as_posix(), size=p.stat().st_size, sha256=common.file_hash(p)) for p in paths]

    def checkpoint(self, stage):
        if not self.request.get('archiveAck'):
            return
        checkpoint_id = uuid.uuid4().hex
        emit('checkpoint', stage=stage, checkpointId=checkpoint_id,
             manifestPath='platform-state.json', **self.state.get('result', {}))
        # Background mode waits only for a frozen local snapshot. Remote commit
        # is separately acknowledged, and must still precede destructive pruning.
        suffix = '.ready' if self.request.get('backgroundArchive') else ''
        ack = self.output / '.archive-acks' / (checkpoint_id + suffix)
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

    def training_preview(self, snapshot, identity):
        """Publish a compact, immutable viewer copy separately from resume state."""
        folder = self.path(self.state['stages']['training'])
        checkpoint = self.engine.checkpoint(snapshot)
        step = checkpoint.step
        preview = folder / 'previews' / f'step-{max(0, step):09d}-{identity[:12]}.sog'
        if preview.is_file():
            gaussian_count(preview, self.cap)
            return
        source = checkpoint.preview
        if not source.is_file():
            emit('log', stage='training', message='This checkpoint has no renderable scene for a SOG preview.')
            return
        # A failed converter must never expose a partial preview or invalidate
        # the native recovery checkpoint. Conversion reads only frozen bytes.
        temporary = folder / '.worker' / ('preview-' + uuid.uuid4().hex)
        temporary.mkdir(parents=True)
        candidate = temporary / 'scene.sog'
        preview.parent.mkdir(parents=True, exist_ok=True)
        try:
            with contextlib.redirect_stdout(sys.stderr):
                log = folder / 'previews' / (preview.stem + '.log')
                source = self.engine.preview_ply(source, temporary, log)
                common.export_sog(source, candidate, log, self.cap)
            gaussian_count(candidate, self.cap)
            candidate.replace(preview)
            sidecar = candidate.with_suffix('.ppisp')
            if sidecar.is_file():
                sidecar.replace(preview.with_suffix('.ppisp'))
        except Exception as error:
            emit('log', stage='training', message=f'SOG checkpoint preview could not be saved: {error}')
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def poll_checkpoints(self, process):
        if self.state.get('stage') == 'densification':
            return self.poll_dense_checkpoint(process)
        if self.state.get('stage') != 'training' or (process is not None and time.monotonic() - self.last_checkpoint_poll < 10):
            return
        self.last_checkpoint_poll = time.monotonic()
        folder = self.path(self.state['stages']['training'])
        candidates = [item for item in self.engine.checkpoint_candidates(folder)
                      if 'retained-checkpoints' not in item.path.parts
                      and self.checkpoint_signatures.get(str(item.source)) != (item.source.stat().st_size, item.source.stat().st_mtime_ns)]
        candidates.sort(key=lambda item: item.order, reverse=True)
        if not candidates or (process is not None and process.poll() is not None):
            return
        # Freeze every engine thread before snapshotting. Open native checkpoint
        # files are never accepted, even if their current size looks plausible.
        with (paused_engine(process) if process is not None else contextlib.nullcontext(set())) as open_paths:
            changed = False
            snapshots = self.state.setdefault('checkpoints', [])
            hashes = {item['sha256'] for item in snapshots}
            for native in candidates:
                candidate = native.source
                if snapshots:
                    previous = self.path(snapshots[-1]['path'])
                    if native.step < self.engine.checkpoint(previous).step:
                        continue
                if candidate.is_symlink() or native.path.is_symlink():
                    raise ValueError('Native checkpoint contains a symlink')
                if candidate.resolve() in open_paths or candidate.stat().st_size == 0:
                    continue
                identity = common.file_hash(candidate)
                if identity in hashes:
                    self.checkpoint_signatures[str(candidate)] = (candidate.stat().st_size, candidate.stat().st_mtime_ns)
                    continue
                if native.config is not None:
                    if native.config.is_symlink():
                        raise ValueError('Native checkpoint configuration is a symlink')
                    if not native.config.is_file() or native.config.resolve() in open_paths:
                        continue
                if native.path.is_dir():
                    # Snapshot the complete directory only while all members are closed.
                    native_directory = native.path.resolve()
                    if any(path == native_directory or path.is_relative_to(native_directory) for path in open_paths):
                        continue
                    if any(path.is_symlink() for path in native.path.rglob('*')):
                        raise ValueError('Native checkpoint contains a symlink')
                if not self.engine.valid_checkpoint(native, self.cap):
                    continue
                snapshot = folder / 'retained-checkpoints' / uuid.uuid4().hex / native.path.name
                snapshot.parent.mkdir(parents=True)
                if native.path.is_dir():
                    shutil.copytree(native.path, snapshot)
                else:
                    shutil.copy2(native.path, snapshot)
                recorded = {'path': snapshot.relative_to(self.output).as_posix(), 'sha256': identity}
                if native.config is not None:
                    config = self.engine.checkpoint(snapshot).config
                    shutil.copy2(native.config, config)
                    recorded['configSha256'] = common.file_hash(config)
                self.training_preview(snapshot, identity)
                self.state['checkpoints'] = [recorded]
                hashes.add(identity)
                self.checkpoint_signatures[str(candidate)] = (candidate.stat().st_size, candidate.stat().st_mtime_ns)
                changed = True
                break
            if changed:
                plan = training_retention_plan(self.output, self.state, engine=self.engine)
                self.state['retention'] = dict(version=1, excludePaths=plan['excludePaths'])
                self.state.setdefault('stageCheckpoints', {})['training'] = dict(directory=folder.name,
                    files=self.file_manifest([*files_under(snapshot.parent), *files_under(folder / 'previews')]))
                self.save()
                self.checkpoint('training')
                if self.request.get('archiveAck') and not self.request.get('backgroundArchive'):
                    self.prune_training(plan, open_paths, candidate if process is not None else None)

    def poll_dense_checkpoint(self, process):
        """Only closed, readable chunk archives are safe native resume points."""
        if process is None or process.poll() is not None or time.monotonic() - self.last_checkpoint_poll < 10:
            return
        self.last_checkpoint_poll = time.monotonic()
        folder = self.path(self.state['stages']['densification'])
        with paused_engine(process) as open_paths:
            import zipfile
            candidates = [p for p in folder.rglob('*.npz') if p.resolve() not in open_paths and not p.is_symlink()]
            signature = [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(candidates)]
            if signature == self.dense_checkpoint_signature:
                return
            chunks = []
            for chunk in candidates:
                try:
                    with zipfile.ZipFile(chunk) as archive:
                        if not {'xyz.npy', 'rgb.npy', 'metadata_json.npy'}.issubset(archive.namelist()) or archive.testzip() is not None:
                            continue
                    chunks.append(chunk)
                except (OSError, zipfile.BadZipFile):
                    continue
            if not chunks or not (folder / 'config.json').is_file():
                return
            files = [folder / 'config.json', *folder.glob('sparse/0/*.bin'), *chunks]
            if any(p.resolve() in open_paths for p in files):
                return
            manifest = dict(directory=folder.name, files=self.file_manifest(files))
            if self.state.get('stageCheckpoints', {}).get('densification') == manifest:
                return
            self.state.setdefault('stageCheckpoints', {})['densification'] = manifest
            self.save()
            self.checkpoint('densification')
            self.dense_checkpoint_signature = signature

    def prune_training(self, plan, open_paths=(), active_checkpoint=None):
        """Called only after the replacement checkpoint's archive is acknowledged."""
        latest = self.path(plan['latestCheckpoint']['path']) if plan.get('latestCheckpoint') else None
        latest_checkpoint = self.engine.checkpoint(latest) if latest else None
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
                if path.suffix == self.engine.checkpoint_suffix and 'retained-checkpoints' not in path.parts:
                    checkpoint = self.engine.checkpoint(path)
                    if not checkpoint.source.is_file() or checkpoint.step > latest_checkpoint.step:
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
        if previous and resumable and name in self.state.get('stageCheckpoints', {}):
            self.restore(self.path(previous))
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
            preserve_final_ply(directory, self.backend, engine=self.engine)
            self.poll_checkpoints(None)
            plan = training_retention_plan(self.output, self.state, engine=self.engine)
            self.state['retention'] = dict(version=1, excludePaths=plan['excludePaths'])
        files = [{'path': p.relative_to(self.output).as_posix(), 'sha256': common.file_hash(p),
                  'size': p.stat().st_size} for p in files_under(directory)
                 if not excluded_path(p.relative_to(self.output).as_posix(), self.state.get('retention', {}).get('excludePaths', []))]
        if not files:
            raise RuntimeError(f'{name} produced no artifacts')
        self.state['completed'][name] = dict(directory=directory.name, files=files)
        if name == 'densification':
            self.state['completed'][name]['files'] += self.file_manifest(files_under(directory.with_name(directory.name + '-dataset')))
        self.state.setdefault('stageCheckpoints', {}).pop(name, None)
        self.save()
        emit('stage', stage=name, status='completed', elapsedSeconds=time.monotonic() - started)
        self.checkpoint(name)
        if name == 'training' and self.request.get('archiveAck') and not self.request.get('backgroundArchive'):
            self.prune_training(plan)
        return directory

    def run(self):
        # A resumed final snapshot no longer needs the uncompressed training PLY.
        finished = self.state['completed'].get('export')
        trained = self.state['completed'].get('training')
        if finished:
            self.restore(self.path(finished['directory']) / 'result.sog')
            ply = None
        elif trained:
            final = self.path(trained['directory']) / 'final'
            self.restore(final)
            ply = self.engine.training_output(self.path(trained['directory'])).ply
            if not ply.is_file():
                raise ValueError('Saved training output is missing')
        else:
            ply = self.engine.run(self)
        expected = gaussian_count(self.path(finished['directory']) / 'result.sog' if finished else ply, self.cap)
        def export(folder):
            folder.mkdir()
            with contextlib.redirect_stdout(sys.stderr):
                self.progress_values = dict(substep='Creating SOG export', gaussians=expected)
                common.export_sog(ply, folder / 'result.sog', folder / 'sog.log', self.cap, on_progress=self.report_log)
            if gaussian_count(folder / 'result.sog', self.cap) != expected:
                raise ValueError('SOG Gaussian count changed during conversion')
        exports = self.stage('export', export)
        count = gaussian_count(exports / 'result.sog', self.cap)
        if count != expected:
            raise ValueError('Export Gaussian counts disagree')
        self.state['result'] = dict(sog=(exports / 'result.sog').relative_to(self.output).as_posix(),
                                    gaussians=count)
        # Completed stage products remain durable for the scan's lifetime.
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
    parser.add_argument('--runtime-versions', choices=tuple(ENGINES))
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
                report = preflight((get_engine(request.get('backend')),))
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
