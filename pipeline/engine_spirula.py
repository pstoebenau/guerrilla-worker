"""Spirula settings, prerequisites, commands and native checkpoint layout."""
import json
from pathlib import Path
import shutil
import tarfile
import uuid

import pipeline_common as common
from engine import Checkpoint, TrainingOutput, checkpoint_step

SPIRULA_VERSION = 'v2026.9.30'


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


class SpirulaEngine:
    name = 'spirula'
    checkpoint_suffix = '.ckpt'
    requires_cuda = False

    def validate_settings(self, settings, cap):
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

    def runtime_versions(self, recorded):
        versions = {key: value for key, value in recorded.items() if key.startswith('spirula')}
        executable = common.spirula_path()
        if not executable.is_file():
            raise RuntimeError('Spirula executable is unavailable')
        versions['spirula'] = SPIRULA_VERSION
        versions['spirulaBinarySha256'] = common.file_hash(executable)
        return versions

    def preflight(self):
        from gpu_runtime import probe_spirula
        return probe_spirula(common.spirula_path())

    def checkpoint(self, path):
        return Checkpoint(path, path / 'state.tar', path / 'splat.ply', checkpoint_step(path), path.parent / 'config.json')

    def checkpoint_candidates(self, folder):
        return (self.checkpoint(path.parent) for path in folder.rglob('state.tar'))

    def valid_checkpoint(self, checkpoint, cap):
        return resumable_spirula_checkpoint(checkpoint.source, cap)

    def training_output(self, folder):
        candidates = list((folder / 'final').glob('*.ply'))
        if candidates:
            return TrainingOutput(candidates[0], folder / 'final/config.json')
        candidates = list((folder / 'run').glob('step-*.ckpt/splat.ply'))
        if not candidates:
            raise RuntimeError('Spirula training produced no splat.ply')
        ply = max(candidates, key=lambda path: self.checkpoint(path.parent).order)
        return TrainingOutput(ply, ply.parent.parent / 'config.json')

    def preview_ply(self, source, temporary, log):
        return source

    def run(self, runner):
        executable = common.spirula_path()
        extraction = {'skip': 10, 'keep': 5, 'max_frames': 300, 'quality': 95, 'scale': 1,
                      **runner.request['settings'].get('extraction', {})}
        flags = [part for key, value in extraction.items() for part in ('--' + key.replace('_', '-'), str(value))]
        def extract(folder):
            runner.command([executable, 'sam', 'extract', runner.source, '--out', folder, *flags], runner.output / f'{folder.name}.log')
            if len([p for p in folder.rglob('*') if p.suffix.lower() in common.IMAGE_SUFFIXES]) < 3:
                raise RuntimeError('Spirula extraction produced fewer than three images')
        selected = runner.stage('selection', extract)
        def reconstruct(folder):
            runner.restore(selected)
            runner.command([executable, 'sfm', 'auto', selected, '-o', folder,
                          '--progress-dir', folder / 'progress', '--no-masks'], runner.output / f'{folder.name}.log')
            if not list(folder.rglob('cameras.bin')) or not list(folder.rglob('points3D.bin')):
                raise RuntimeError('Spirula did not produce a COLMAP reconstruction')
            # sfm auto writes sparse/features/matches, not a copy of its images.
            shutil.copytree(selected, folder / 'images', dirs_exist_ok=True)
        dataset = runner.stage('reconstruction', reconstruct, resumable=True)
        def train(folder):
            runner.restore(dataset / 'images', dataset / 'sparse')
            folder.mkdir(parents=True, exist_ok=True)
            checkpoints = [runner.path(item['path']) for item in runner.state.get('checkpoints', [])
                           if item['path'].endswith('.ckpt')]
            if not checkpoints and (folder / 'run').exists():
                (folder / 'run').rename(folder / ('failed-run-' + uuid.uuid4().hex[:12]))
            command = [executable, 'train', '--data', dataset, '--data-format', 'colmap',
                       '--output-dir-prefix', folder, '--output-dir-name', 'run',
                       '--cap-max', runner.cap, '--num-iterations', runner.request['settings'].get('training', {}).get('iterations', 30000),
                       '--save-full-checkpoint', '1', '--save-only-latest-checkpoint', '0',
                       '--disable-viewer', '1', '--keep-viewer-alive', '0']
            if checkpoints:
                command += ['--resume', checkpoints[-1]]
            runner.command(command, runner.output / f'{folder.name}.log')
        training = runner.stage('training', train, resumable=True)
        return self.training_output(training).ply

