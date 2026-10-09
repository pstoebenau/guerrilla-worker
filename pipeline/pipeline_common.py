"""Helpers shared by the desktop and Docker pipelines: runtime paths, hashing,
selection manifests, logged subprocesses, LichtFeld training and SPZ export."""
from __future__ import annotations

import hashlib
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
import uuid

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


def studio_path():
    """LichtFeld executable: LICHTFELD_BIN, else the desktop install or the container path."""
    override = os.environ.get("LICHTFELD_BIN")
    if override:
        return Path(override)
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        return local / "Programs/LichtFeld Studio/bin/LichtFeld-Studio.exe"
    return Path("/opt/lichtfeld/bin/run_lichtfeld.sh")


def densification_plugin_path():
    return Path(os.environ.get("LICHTFELD_DENSIFICATION_PLUGIN",
                               str(Path.home() / ".lichtfeld/plugins/densification")))


def densify(studio, dataset, output, max_cap, settings=None, on_progress=None, on_tick=None, portable_checkpoints=False):
    """Run installed RoMaV2 High in a fresh attempt; publish only validated points."""
    import sys
    plugin = densification_plugin_path().resolve()
    python = Path(studio).parent / 'python.exe' if os.name == 'nt' else Path(sys.executable)
    for path in (python, plugin / 'densify.py'):
        if not path.is_file():
            raise FileNotFoundError(path)
    bootstrap = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                 "from densification_worker import main; main()")
    output = Path(output)
    log = output.parent / (output.name + '.log')
    if log.exists():
        log = output.parent / (output.name + '-resume-' + time.strftime('%Y%m%d-%H%M%S') + '.log')
    command = [python, '-u', '-c', bootstrap, Path(__file__).resolve().parent,
               '--plugin', plugin, '--dataset', dataset, '--output', output, '--max-cap', max_cap]
    if settings is not None:
        command += ['--settings-json', json.dumps(settings)]
    if output.exists():
        command.append('--resume')
    if portable_checkpoints:
        command.append('--portable-checkpoints')
    run_logged(command, log, **({'on_progress': on_progress} if on_progress else {}), **({'on_tick': on_tick} if on_tick else {}))
    return json.loads((Path(output) / 'result.json').read_text(encoding='utf-8'))


def save_json(path, data):
    """Publish atomically, tolerating brief Windows reader/antivirus file locks."""
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        for attempt in range(6):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * 2 ** attempt)
    finally:
        temporary.unlink(missing_ok=True)


def file_hash(path):
    """Plain SHA-256 of one file's contents."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(paths):
    """Digest of an ordered file set, delimited by name and size so boundaries cannot collide."""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(json.dumps([path.name, path.stat().st_size]).encode())
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def selected_images(folder):
    """Images listed in selection.json, else every image in the folder; at least three flat, real files."""
    folder = Path(folder)
    manifest = folder / "selection.json"
    if manifest.is_file():
        report = json.loads(manifest.read_text(encoding="utf-8"))
        names = [item["filename"] for item in report["selected"]]
    else:
        names = sorted(p.name for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    paths = [folder / name for name in names]
    if (len(names) < 3 or len(names) != len(set(names))
            or any(not name or Path(name).name != name or "/" in name or "\\" in name for name in names)
            or any(not p.is_file() or p.is_symlink() for p in paths)):
        raise ValueError("Reconstruction requires at least three distinct, existing flat selected images")
    return paths


def run_logged(command, log, on_tick=None, on_progress=None):
    """Run a child process with stdout/stderr in `log`; kill it on interruption; raise with a log tail on failure."""
    command = [str(x) for x in command]
    print("Running: " + subprocess.list2cmdline(command), flush=True)
    if log.exists():
        log.replace(log.with_name(log.stem + '-previous-' + uuid.uuid4().hex[:12] + log.suffix))
    child_options = {}
    if os.name != 'nt' and os.environ.get('PIPELINE_GPU_LOCK_FD'):
        child_options['pass_fds'] = (int(os.environ['PIPELINE_GPU_LOCK_FD']),)
        # Each supervised generation dies with its parent, including SIGKILL.
        # Resolve libc before fork; the child callback only calls libc/syscalls.
        libc = ctypes.CDLL(None, use_errno=True)
        parent_pid = os.getpid()
        def parent_death_signal():
            if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
                os._exit(1)
        child_options['preexec_fn'] = parent_death_signal
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                   start_new_session=os.name != "nt",
                                   **child_options,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        previous = {}

        def interrupted(signum, _frame):
            raise InterruptedError(f"Pipeline interrupted by signal {signum}")

        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, interrupted)
            while process.poll() is None:
                if on_progress:
                    on_progress(log)
                if on_tick:
                    on_tick(process)
                time.sleep(0.25)
        except BaseException:
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    if on_progress:
        on_progress(log, force=True)
    if process.returncode:
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 4000))
            tail = stream.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Command exited {process.returncode}; see {log}\n{tail}")


def find_ply(folder):
    """The highest-iteration splat_ITER.ply under `folder`."""
    candidates = [(int(match[1]), p) for p in folder.rglob("splat_*.ply")
                  if (match := re.fullmatch(r"splat_(\d+)\.ply", p.name)) and p.stat().st_size > 100]
    if not candidates:
        raise RuntimeError(f"Training produced no splat_ITER.ply in {folder}")
    return max(candidates, key=lambda item: item[0])[1]


def train(studio, dataset, output, config, extra=(), on_tick=None, on_progress=None):
    """Headless LichtFeld training into a new `output` folder; returns the final PLY."""
    output.mkdir(parents=True, exist_ok=False)
    save_json(output / "config.json", config)
    flags = [flag for key, flag in [('gut', '--gut'), ('use_bilateral_grid', '--bilateral-grid'),
                                  ('mip_filter', '--enable-mip'), ('use_ppisp', '--ppisp'),
                                  ('enable_sparsity', '--enable-sparsity'), ('undistort', '--undistort')]
             if config.get(key)]
    run_logged([studio, "--headless", "--train", "-d", dataset, "-o", output,
                "--config", output / "config.json", "--max-cap", str(config["max_cap"]),
                *flags, "-r", "1", "--max-width", "0", *extra], output / "training.log", **({'on_tick': on_tick} if on_tick else {}),
                **({'on_progress': on_progress} if on_progress else {}))
    ply = find_ply(output)
    expected = config['iterations'] + (config.get('sparsify_steps', 15000)
                                       if config.get('enable_sparsity') else 0)
    if ply.name != f'splat_{expected}.ply':
        raise RuntimeError(f'Training did not export the final {expected}-step result: {ply}')
    return ply


def export_spz(studio, ply, spz, log, on_progress=None):
    """Convert `ply` to `spz` (new file) and copy the PPISP sidecar beside it when present."""
    if spz.exists():
        raise FileExistsError(f"Refusing to replace {spz}")
    run_logged([studio, "convert", ply, spz], log, **({"on_progress": on_progress} if on_progress else {}))
    if not spz.is_file() or spz.stat().st_size < 100:
        raise RuntimeError("SPZ export is missing or empty")
    sidecar = Path(ply).with_suffix(".ppisp")
    if sidecar.exists():
        shutil.copy2(sidecar, spz.with_suffix(".ppisp"))
    return spz


def export_sog(studio, ply, sog, log, max_cap, on_progress=None):
    """Export the default SOG and verify its embedded Gaussian count."""
    from scan_transfer import gaussian_count
    expected = gaussian_count(ply, max_cap)
    if not sog.exists():
        run_logged([studio, 'convert', ply, sog], log, **({'on_progress': on_progress} if on_progress else {}))
    count = gaussian_count(sog, max_cap)
    if count != expected:
        raise ValueError(f'SOG export count changed: {expected} -> {count}')
    sidecar = Path(ply).with_suffix('.ppisp')
    if sidecar.exists():
        shutil.copy2(sidecar, sog.with_suffix('.ppisp'))
    return sog


def remove_training_plys(training):
    """Discard successful training PLY intermediates after verified exports."""
    training = Path(training).resolve()
    for path in training.rglob('splat_*.ply'):
        if path.is_symlink() or not path.resolve().is_relative_to(training):
            raise ValueError('Training PLY cleanup path escapes the training folder')
        path.unlink()
