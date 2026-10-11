"""LichtFeld settings, prerequisites, commands and native checkpoint layout."""
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pipeline_common as common
import scan_settings
from engine import Checkpoint, TrainingOutput, checkpoint_step

ROOT = Path(__file__).resolve().parent


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


class LichtFeldEngine:
    name = 'lichtfeld'
    checkpoint_suffix = '.resume'
    requires_cuda = True

    def validate_settings(self, settings, cap):
        from jsonschema import Draft7Validator
        Draft7Validator(json.loads(scan_settings.SCHEMA_PATH.read_text())).validate(settings)
        if settings.get('training', {}).get('max_cap', cap) != cap:
            raise ValueError('Training max_cap must equal the requested maximum')

    def runtime_versions(self, recorded):
        # Legacy manifests have unprefixed reconstruction/model fields used by this engine.
        keys = ('lichtfeldArtifactSha256', 'reconstructionEngine', 'densificationPlugin',
                'romaWeightsSha256', 'dinov3Commit', 'dinov3SourceSha256', 'pythonEnvironmentSha256')
        versions = {key: value for key, value in recorded.items() if key in keys}
        studio = common.studio_path()
        if not studio.is_file():
            raise RuntimeError('LichtFeld executable is unavailable')
        versions['lichtfeld'] = subprocess.check_output([str(studio), '--version'], text=True, stderr=subprocess.STDOUT).strip()
        if os.name == 'nt':
            versions['mode'] = 'native-development'
            plugin = common.densification_plugin_path()
            sources = sorted(path for path in plugin.rglob('*.py') if '.venv' not in path.parts and '__pycache__' not in path.parts)
            if sources:
                versions['densificationPlugin'] = common.fingerprint(sources)
        return versions

    def preflight(self):
        if sys.platform == 'darwin':
            raise RuntimeError('LichtFeld requires NVIDIA CUDA; this Mac worker supports Spirula only')
        from colmap_worker import probe as probe_colmap
        probe_colmap(scan_settings.defaults()['reconstruction'])
        if os.name == 'nt':
            studio_python = common.studio_path().parent / 'python.exe'
            probe = ('import sys,site; from pathlib import Path; '
                     'p=Path(sys.argv[1]); site.addsitedir(str(p/".venv/Lib/site-packages")); '
                     'import torch,lichtfeld; assert torch.cuda.is_available()')
            subprocess.run([str(studio_python), '-c', probe, str(common.densification_plugin_path())],
                           capture_output=True, text=True, check=True, timeout=60)
            return
        import pycolmap
        import torch
        if not pycolmap.has_cuda or not torch.cuda.is_available():
            raise RuntimeError('CUDA reconstruction/training is unavailable')
        plugin = common.densification_plugin_path()
        if not (plugin / 'densify.py').is_file():
            raise RuntimeError(f'Missing Linux plugin: {plugin.name}')
        import lichtfeld  # noqa: F401 — native plugin dependency must load
        import importlib
        sys.path.insert(0, str(plugin.parent))
        importlib.import_module(plugin.name + '.densify')

    def checkpoint(self, path):
        return Checkpoint(path, path, path, checkpoint_step(path))

    def checkpoint_candidates(self, folder):
        return (self.checkpoint(path) for path in folder.rglob('*.resume'))

    def valid_checkpoint(self, checkpoint, cap):
        return checkpoint.source.is_file() and checkpoint.source.stat().st_size > 0

    def training_output(self, folder):
        final = folder / 'final'
        selected = final if final.exists() else folder
        return TrainingOutput(common.find_ply(selected), selected / 'config.json')

    def preview_ply(self, source, temporary, log):
        ply = temporary / 'checkpoint.ply'
        common.run_logged([common.studio_path(), 'convert', source, ply], log)
        return ply

    def run(self, runner):
        options_file = runner.output / 'settings.json'
        common.save_json(options_file, runner.request['settings'])
        options = scan_settings.load(options_file)
        selected = runner.stage('selection', lambda folder: runner.command(
            [sys.executable, ROOT / 'select_frames.py', runner.source, folder,
             *scan_settings.selection_arguments(options), '--image-format', 'jpg', '--jpeg-quality', '95'], runner.output / f'{folder.name}.log'))

        def reconstruct(folder):
            runner.restore(selected)
            runner.command([sys.executable, ROOT / 'reconstruct_splat.py', '--images', selected,
                          '--output', folder, '--settings', options_file, '--reconstruction-only',
                          '--retain-artifacts', '--max-cap', runner.cap], runner.output / f'{folder.name}.log')
        reconstructed = runner.stage('reconstruction', reconstruct)
        # Dataset paths in the desktop manifest are not used here: scratch is relocatable.
        def densify(folder):
            result_file = reconstructed / 'colmap-result.json'
            runner.restore(result_file)
            result = json.loads(result_file.read_text()) if result_file.exists() else {}
            dataset = runner.path(result.get('dataset_relative', 'dataset'), root=reconstructed)
            runner.restore(dataset / 'images', dataset / 'sparse')
            # Densification publishes into a copy, keeping reconstruction immutable.
            staged_dataset = folder.parent / (folder.name + '-dataset')
            if not staged_dataset.exists():
                shutil.copytree(dataset, staged_dataset)
            relocate_densification_config(folder, staged_dataset)
            with contextlib.redirect_stdout(sys.stderr):
                common.densify(common.studio_path(), staged_dataset, folder, runner.cap,
                               settings=scan_settings.densification(options), on_progress=runner.report_log, on_tick=runner.poll_checkpoints,
                               portable_checkpoints=True)
        dense = runner.stage('densification', densify, resumable=True)
        train_dataset = dense.parent / (dense.name + '-dataset')
        training = runner.stage('training', lambda folder: self.train(runner, folder, train_dataset, options), resumable=True)
        return self.training_output(training).ply

    def train(self, runner, folder, dataset, options):
        runner.restore(dataset / 'images', dataset / 'sparse')
        runner.active_log = folder / 'training.log'
        config = scan_settings.training(options, runner.cap)
        checkpoints = [runner.path(item['path']) for item in runner.state.get('checkpoints', [])
                       if item['path'].endswith('.resume')]
        if not folder.exists():
            with contextlib.redirect_stdout(sys.stderr):
                common.train(common.studio_path(), dataset, folder, config, on_tick=runner.poll_checkpoints, on_progress=runner.report_log)
            return
        if not checkpoints:
            # Preserve failed work; a new directory is a distinct training attempt.
            archived = folder.with_name(folder.name + '-failed-' + uuid.uuid4().hex[:8])
            folder.rename(archived)
            with contextlib.redirect_stdout(sys.stderr):
                common.train(common.studio_path(), dataset, folder, config, on_tick=runner.poll_checkpoints, on_progress=runner.report_log)
            return
        runner.command([common.studio_path(), '--headless', '--train', '--resume', checkpoints[-1],
                      '-d', dataset, '-o', folder, '--max-cap', runner.cap], folder / 'resume.log')
        ply = common.find_ply(folder)
        expected = config['iterations'] + (config['sparsify_steps'] if config['enable_sparsity'] else 0)
        if ply.name != f'splat_{expected}.ply':
            raise RuntimeError('Resumed LichtFeld did not finish the configured training duration')
