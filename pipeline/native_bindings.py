"""Validated single-operation boundary for native Python integrations.

Rust owns stage state, subprocesses, retries, checkpoint policy and export.
These operations only call the established OpenCV/COLMAP/LichtFeld bindings.
Native library typing is isolated behind the explicit stubs in typing/.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import site
from pathlib import Path
import sys
from typing import TypeAlias, cast

Json: TypeAlias = str | int | float | bool | None | list['Json'] | dict[str, 'Json']
_dll_directories: list[object] = []


def object_value(value: Json, name: str) -> dict[str, Json]:
    if not isinstance(value, dict):
        raise ValueError(f'{name} must be an object')
    return value


def text_value(data: dict[str, Json], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f'{name} must be a nonempty string')
    return value


def existing_path(data: dict[str, Json], name: str) -> Path:
    result = Path(text_value(data, name))
    if result.is_symlink() or not result.exists():
        raise ValueError(f'{name} is not an existing regular path')
    return result.resolve()


def configure_dependencies(request: dict[str, Json]) -> None:
    if os.name == 'nt' and (crt_root := os.environ.get('WORKER_CRT_ROOT')):
        crt = Path(crt_root)
        if not crt.is_absolute() or not crt.is_dir():
            raise ValueError('Bundled Windows CRT directory is unavailable')
        # Studio supplies its own Python API interpreter. Keep this DLL search
        # handle alive so native libraries still resolve the worker's CRT bundle.
        _dll_directories.append(os.add_dll_directory(str(crt)))
    if 'engineBin' in request:
        engine_bin = existing_path(request, 'engineBin')
        if not engine_bin.is_dir():
            raise ValueError('Engine Python API directory must be a directory')
        # LichtFeld's own extension lives beside its owner-installed executable.
        # Append this trusted request path without replacing worker dependencies
        # or relying on an ambient shell PYTHONPATH.
        if str(engine_bin) not in sys.path:
            sys.path.append(str(engine_bin))
    if 'pythonSitePackages' not in request:
        return
    packages = existing_path(request, 'pythonSitePackages')
    version = request.get('pythonVersion')
    if not isinstance(version, list) or version != list(sys.version_info[:2]):
        raise ValueError('LichtFeld Python API ABI differs from the bundled Python runtime; use a compatible engine installation')
    site.addsitedir(str(packages))
    if str(packages) in sys.path:
        sys.path.remove(str(packages))
    sys.path.insert(0, str(packages))


def execute(request: dict[str, Json]) -> dict[str, Json]:
    action = text_value(request, 'action')
    configure_dependencies(request)
    if action == 'open-files':
        pids = request.get('pids')
        if not isinstance(pids, list) or any(type(pid) is not int for pid in pids):
            raise ValueError('pids must contain integers')
        import psutil
        paths: list[Json] = []
        for pid in pids:
            if not isinstance(pid, int):
                raise ValueError('Invalid process ID')
            paths.extend(str(Path(item.path).resolve()) for item in psutil.Process(pid).open_files())
        return {'paths': paths}
    if action == 'probe':
        from engine_lichtfeld import LichtFeldEngine
        LichtFeldEngine().preflight()
        return {'available': True}
    if action == 'selection':
        source = existing_path(request, 'source')
        output = Path(text_value(request, 'output'))
        options = object_value(request.get('options'), 'options')
        arguments = [str(source), str(output), '--image-format', 'jpg', '--jpeg-quality', '95']
        for key, value in options.items():
            flag = '--' + key.replace('_', '-')
            if value is True:
                arguments.append(flag)
            elif value is not None and value is not False:
                arguments.extend((flag, str(value)))
        from select_frames import main as select
        select(arguments)
        return {'output': str(output)}
    if action == 'reconstruction':
        images = existing_path(request, 'images')
        output = Path(text_value(request, 'output'))
        options = object_value(request.get('options'), 'options')
        output.mkdir(parents=True, exist_ok=True)
        from colmap_worker import probe, reconstruct
        probe(options)
        reconstruct(images, output, options, retain_artifacts=True)
        result = object_value(cast(Json, json.loads((output / 'colmap-result.json').read_text())), 'COLMAP result')
        existing_path({'dataset': result.get('dataset_dir')}, 'dataset')
        return {'output': str(output)}
    if action == 'densification':
        dataset = existing_path(request, 'dataset')
        plugin = existing_path(request, 'plugin')
        output = Path(text_value(request, 'output'))
        cap = request.get('maxCap')
        if type(cap) is not int or cap < 1:
            raise ValueError('maxCap must be a positive integer')
        options = object_value(request.get('options'), 'options')
        from densification_worker import main as densify
        original = sys.argv
        sys.argv = ['densification_worker', '--plugin', str(plugin), '--dataset', str(dataset),
                    '--output', str(output), '--max-cap', str(cap), '--settings-json', json.dumps(options),
                    '--portable-checkpoints']
        if output.exists():
            sys.argv.append('--resume')
        try:
            densify()
        finally:
            sys.argv = original
        return {'output': str(output)}
    raise ValueError(f'Unsupported native operation: {action}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    args = parser.parse_args()
    protocol = sys.stdout
    try:
        request = object_value(cast(Json, json.loads(args.request.read_text(encoding='utf-8'))), 'request')
        with contextlib.redirect_stdout(sys.stderr):
            result = execute(request)
        print(json.dumps({'version': 1, 'ok': True, 'result': result}, allow_nan=False), file=protocol, flush=True)
        return 0
    except Exception as error:
        print(json.dumps({'version': 1, 'ok': False, 'error': str(error)[:2000]}), file=protocol, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
