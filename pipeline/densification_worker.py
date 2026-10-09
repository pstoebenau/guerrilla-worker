"""Run the installed RoMaV2 plugin without changing its UI defaults or sparse model."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import shutil
import site
import struct
import sys

import pipeline_common as common
from pipeline_defaults import DENSIFICATION


def portable_chunk_fingerprints(module, dataset):
    """Enrolled scans relocate scratch on every attempt; native chunk identity must not."""
    original = getattr(module, '_camera_record_fingerprint', None)
    if not callable(original):
        raise RuntimeError('Densification plugin does not support portable chunk checkpoints')
    def fingerprint(record):
        result = original(record)
        for key in ('image_path', 'mask_path'):
            if result.get(key) is not None:
                file = Path(result[key]).resolve()
                if not file.is_relative_to(dataset):
                    raise ValueError('Densification checkpoint input is outside its dataset')
                result[key] = file.relative_to(dataset).as_posix()
        return result
    module._camera_record_fingerprint = fingerprint


def publish(report, source, target):
    """Keep the plugin's original sparse PLY, then atomically install dense points."""
    if target.exists() and common.file_hash(target) != report['sha256']:
        original_hash = report.get('original_pointcloud_sha256')
        if common.file_hash(target) != original_hash:
            raise ValueError('Published point cloud changed')
        backup = target.with_name('points3D-sparse.ply')
        if backup.exists() and common.file_hash(backup) != original_hash:
            raise ValueError('Sparse point cloud backup changed')
        if not backup.exists():
            shutil.copy2(target, backup)
    if not target.exists() or common.file_hash(target) != report['sha256']:
        temporary = target.with_suffix('.ply.tmp')
        shutil.copy2(source, temporary)
        temporary.replace(target)
    common.save_json(target.parent / 'densification.json', report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-cap', type=int, required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--portable-checkpoints', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--settings-json', help=argparse.SUPPRESS)
    args = parser.parse_args()
    settings = json.loads(args.settings_json) if args.settings_json else DENSIFICATION
    if args.max_cap <= 0:
        parser.error('--max-cap must be positive')
    plugin, dataset, output = args.plugin.resolve(), args.dataset.resolve(), args.output.resolve()
    # Filtered/service environments may lack USERNAME; getpass then imports Unix-only
    # pwd on Windows. Use PyTorch's supported cache override before importing RoMa.
    # Keep it outside the attempt, whose existence controls fresh/resume behavior.
    os.environ.setdefault('TORCHINDUCTOR_CACHE_DIR', str(output.parent / '.cache/torchinductor'))
    site.addsitedir(str(plugin / '.venv/Lib/site-packages'))
    sys.path.insert(0, str(plugin.parent))
    module = importlib.import_module(plugin.name + '.densify')
    if args.portable_checkpoints:
        portable_chunk_fingerprints(module, dataset)
    sparse = dataset / 'sparse'
    if not (sparse / 'cameras.bin').is_file():
        sparse = sparse / '0'
    target = sparse / 'points3D.ply'
    if args.resume and (output / 'result.json').is_file():
        report = json.loads((output / 'result.json').read_text(encoding='utf-8'))
        if report['settings'] != dict(settings, max_points=args.max_cap):
            raise ValueError('Densification settings changed')
        source = output / 'sparse/0/points3D.ply'
        if common.file_hash(source) != report['sha256']:
            raise ValueError('Completed dense point cloud changed')
        publish(report, source, target)
        return
    original_hash = None
    if target.exists():
        from scan_transfer import gaussian_count
        with (sparse / 'points3D.bin').open('rb') as stream:
            sparse_count = struct.unpack('<Q', stream.read(8))[0]
        if (sparse / 'densification.json').exists() or gaussian_count(target, sparse_count) != sparse_count:
            raise FileExistsError(f'Refusing to overwrite existing custom initialization: {target}')
        original_hash = common.file_hash(target)
    staged = output / 'sparse/0'
    if not args.resume:
        output.mkdir(parents=True, exist_ok=False)
        staged.mkdir(parents=True)
        for path in sparse.glob('*.bin'):
            shutil.copy2(path, staged / path.name)
    else:
        for path in sparse.glob('*.bin'):
            if common.file_hash(path) != common.file_hash(staged / path.name):
                raise ValueError('Densification source model changed')
    command = ['--scene_root', str(output), '--images_subdir', str(dataset / 'images'),
               '--out_name', 'points3D.ply', '--max_points', str(args.max_cap), '--resume_chunks']
    for key, value in settings.items():
        command += ['--' + key, str(value)]
    config = module.build_argparser().parse_args(command)
    if args.resume and json.loads((output / 'config.json').read_text(encoding='utf-8')) != vars(config):
        raise ValueError('Densification arguments changed')
    common.save_json(output / 'config.json', vars(config))
    print(f"RoMaV2 {settings['roma_setting']} densification started", flush=True)
    result = module.dense_init(config, progress_callback=module._cli_progress_callback())
    if result:
        raise RuntimeError(f'RoMaV2 densification exited {result}')
    from scan_transfer import gaussian_count
    ply = staged / 'points3D.ply'
    count = gaussian_count(ply, args.max_cap)
    report = {'plugin': str(plugin), 'settings': dict(settings, max_points=args.max_cap),
              'points': count, 'pointcloud': str(target), 'sha256': common.file_hash(ply),
              'original_pointcloud_sha256': original_hash}
    common.save_json(output / 'result.json', report)
    publish(report, ply, target)
    print(f"RoMaV2 {settings['roma_setting']} complete: {count:,} initialization points", flush=True)


if __name__ == '__main__':
    main()
