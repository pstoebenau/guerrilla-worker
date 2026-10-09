"""Container: one video or ordered photo folder -> selection -> COLMAP -> LichtFeld -> SPZ."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

import pipeline_common as common
from pipeline_defaults import (TRAINING, MAX_IMAGES, SAMPLE_FPS, KEEP_PERCENT, BLUR_RATIO,
                               MAX_GLARE, DENSIFICATION, CALIBRATE_INTRINSICS, EXPORT_FORMAT)
from select_images import image_paths, select_images

ROOT = Path(__file__).resolve().parent
LICHTFELD_VERSION = "v0.5.3 (d8c50c6)"
PIPELINE_VERSION = 3


def input_files(source):
    if source.is_symlink():
        raise ValueError("Input must not be a symlink")
    if source.is_dir():
        return image_paths(source)
    if not source.is_file():
        raise ValueError(f"Input does not exist: {source}")
    return [source]


def validate_paths(source, output):
    if source == output or source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError("Input and output must be separate, non-overlapping paths")


@contextmanager
def output_lock(output, resume):
    output.mkdir(parents=True, exist_ok=True)
    if not resume and any(p.name != ".pipeline.lock" for p in output.iterdir()):
        raise ValueError("Output must be new or empty; use --resume for an existing pipeline job")
    with (output / ".pipeline.lock").open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt
                stream.write(b"0")
                stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Another pipeline process is using this output folder") from exc
        try:
            # Check again after acquiring the lock to close the creation race.
            if not resume and any(p.name != ".pipeline.lock" for p in output.iterdir()):
                raise ValueError("Output is no longer empty; use a different job folder")
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def preflight(studio):
    version = subprocess.check_output([studio, "--version"], text=True, stderr=subprocess.STDOUT).strip()
    # A shallow checkout by immutable commit has no release tag, so upstream's
    # --version uses that revision as both the version name and commit field.
    valid_versions = {f"LichtFeld Studio {LICHTFELD_VERSION}", "LichtFeld Studio d8c50c6 (d8c50c6)"}
    if version not in valid_versions:
        raise ValueError(f"Expected LichtFeld {LICHTFELD_VERSION}, got {version}")
    import pycolmap
    if pycolmap.__version__ != "4.0.2" or not pycolmap.has_cuda:
        raise ValueError("Expected CUDA-enabled pycolmap 4.0.2")
    gpu = subprocess.check_output(["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
                                   "--format=csv,noheader"], text=True).strip()
    print(f"LichtFeld: {version}\nCOLMAP: {pycolmap.__version__} (CUDA)\nGPU: {gpu}", flush=True)
    return {"lichtfeld": version, "pycolmap": pycolmap.__version__, "gpu": gpu}


def owned_path(output, relative):
    path = (output / relative).resolve()
    if path == output or not path.is_relative_to(output):
        raise ValueError("Manifest artifact path escapes the output folder")
    return path


def check_completed(output, state):
    for stage in state["completed"]:
        record = state["artifacts"][stage]
        print(f"[resume] Verifying {stage}: {len(record['files'])} artifact files", flush=True)
        for relative, expected in record["files"].items():
            if stage == 'train' and 'export' in state['completed'] and relative == state.get('ply'):
                continue  # Successful training PLY intermediates are discarded after export.
            path = owned_path(output, relative)
            if not path.is_file() or common.fingerprint([path]) != expected:
                raise ValueError(f"Completed {stage} artifact changed or is missing: {relative}")


def run(args):
    raw_source = args.input.absolute()
    if raw_source.is_symlink():
        raise ValueError("Input must not be a symlink")
    source = raw_source.resolve()
    output = args.output.resolve()
    validate_paths(source, output)
    paths = input_files(source)
    settings = {"pipeline_version": PIPELINE_VERSION, "input_type": "photos" if source.is_dir() else "video",
                "max_images": args.max_images, "sample_fps": SAMPLE_FPS, "video_keep_percent": KEEP_PERCENT,
                "blur_ratio": BLUR_RATIO, "max_glare": MAX_GLARE, "training": TRAINING,
                "photo_keep_percent": 100.0, "lichtfeld": LICHTFELD_VERSION, "pycolmap": "4.0.2"}
    settings.update(densification=DENSIFICATION, calibrate_intrinsics=CALIBRATE_INTRINSICS,
                    export_format=EXPORT_FORMAT)
    if args.check:
        preflight(args.studio)
        print(f"Input: {source} ({len(paths)} source files); output: {output}")
        return
    with output_lock(output, args.resume):
        print(f"Hashing {len(paths)} input file(s)...", flush=True)
        signature = common.fingerprint(paths)
        manifest = output / "pipeline.json"
        if args.resume:
            state = json.loads(manifest.read_text(encoding="utf-8"))
            if state["input_sha256"] != signature or state["settings"] != settings:
                raise ValueError("Input contents or settings changed; start a new output folder")
            check_completed(output, state)
        else:
            state = {"source": str(source), "input_sha256": signature, "settings": settings,
                     "completed": [], "artifacts": {}, "attempts": [], "status": "running"}
        state.pop("error", None)
        state["status"] = "running"
        common.save_json(manifest, state)

        def attempt(stage):
            folder = output / f"{stage}-{uuid.uuid4().hex[:12]}"
            state["stage"] = stage
            state["attempts"].append({"stage": stage, "directory": folder.name, "started": time.time()})
            common.save_json(manifest, state)
            print(f"[{stage}] {folder.name}", flush=True)
            return folder

        def complete(stage, folder, files):
            state["artifacts"][stage] = {"directory": folder.name,
                                         "files": {str(p.relative_to(output)): common.fingerprint([p]) for p in files}}
            state["completed"].append(stage)
            common.save_json(manifest, state)

        try:
            if not args.select_only:
                state["environment"] = preflight(args.studio)
                common.save_json(manifest, state)
            if "select" not in state["completed"]:
                selected = attempt("select")
                if source.is_dir():
                    select_images(source, selected, args.max_images)
                else:
                    command = [sys.executable, ROOT / "select_frames.py", source, selected]
                    if args.max_images:
                        command += ["--max-images", args.max_images]
                    common.run_logged(command, output / f"{selected.name}.log")
                report = json.loads((selected / "selection.json").read_text(encoding="utf-8"))
                # Verify the input was not modified while the selector was reading it.
                if signature != common.fingerprint(input_files(source)):
                    raise ValueError("Source changed during selection; use a new job with stable input")
                files = [selected / item["filename"] for item in report["selected"]]
                if len(files) < 3:
                    raise ValueError("Fewer than three selected images; inspect selection.json/candidates.csv")
                complete("select", selected, files + [selected / "selection.json", selected / "candidates.csv"])
            selected = owned_path(output, state["artifacts"]["select"]["directory"])
            if args.select_only:
                state["status"] = "complete" if "export" in state["completed"] else "selected"
                common.save_json(manifest, state)
                print(f"Selection complete: {selected}; use --resume to reconstruct/train/export")
                return
            if "colmap" not in state["completed"]:
                reconstruction = attempt("colmap")
                common.run_logged([sys.executable, ROOT / "colmap_worker.py", selected, reconstruction],
                                  output / f"{reconstruction.name}.log")
                dataset = owned_path(reconstruction, json.loads((reconstruction / 'colmap-result.json').read_text()).get('dataset_relative', 'dataset'))
                sparse_files = list((dataset / "sparse").rglob("*.bin"))
                if not all(any(p.name == name for p in sparse_files)
                           for name in ("cameras.bin", "images.bin", "points3D.bin")):
                    raise RuntimeError("Undistortion did not produce a complete COLMAP model")
                complete("colmap", reconstruction, sparse_files + list((dataset / "images").glob("*.png"))
                         + [reconstruction / "colmap-result.json"])
            reconstruction = owned_path(output, state["artifacts"]["colmap"]["directory"])
            dataset = owned_path(reconstruction, json.loads((reconstruction / 'colmap-result.json').read_text()).get('dataset_relative', 'dataset'))
            if 'densify' not in state['completed']:
                previous_dense = next((item for item in reversed(state['attempts'])
                                       if item['stage'] == 'densify'), None)
                dense_attempt = (owned_path(output, previous_dense['directory']) if previous_dense
                                 else attempt('densify'))
                dense = common.densify(args.studio, dataset, dense_attempt, TRAINING['max_cap'])
                complete('densify', dense_attempt, [dense_attempt / 'result.json', Path(dense['pointcloud'])])
            if "train" not in state["completed"]:
                training = attempt("train")
                ply = common.train(args.studio, dataset, training, TRAINING)
                state["ply"] = str(ply.relative_to(output))
                complete("train", training, [ply, training / "config.json"] + list(training.glob("*.ppisp")))
            if "export" not in state["completed"]:
                if (output / "result.spz").exists():
                    raise FileExistsError("result.spz already exists but is not a completed export; preserve/move it before retrying")
                export = attempt("export")
                export.mkdir()
                ply = owned_path(output, state['ply'])
                sog = common.export_sog(ply, export / 'result.sog', export / 'export-sog.log', TRAINING['max_cap'])
                spz = common.export_spz(ply, export / "result.spz", export / "export-spz.log")
                # Record the completed export before publishing the stable filename;
                # a crash between these steps is recoverable with --resume.
                complete("export", export, [sog, spz])
            exported = owned_path(output, state["artifacts"]["export"]["directory"]) / "result.spz"
            result = output / "result.spz"
            if result.exists():
                if common.fingerprint([result]) != common.fingerprint([exported]):
                    raise ValueError("result.spz differs from the completed export; refusing to replace it")
            else:
                # Publish only a complete file; an interrupted copy leaves a uniquely
                # named temporary file and is safe to retry under the job lock.
                pending = output / f".result-{uuid.uuid4().hex}.spz"
                shutil.copy2(exported, pending)
                pending.replace(result)
            sidecar = owned_path(output, state["ply"]).with_suffix(".ppisp")
            if sidecar.exists():
                shutil.copy2(sidecar, output / "result.ppisp")
            sog_source = owned_path(output, state['artifacts']['export']['directory']) / 'result.sog'
            sog_result = output / 'result.sog'
            if sog_result.exists():
                if common.fingerprint([sog_result]) != common.fingerprint([sog_source]):
                    raise ValueError('result.sog differs from the completed export')
            else:
                pending_sog = output / f'.result-{uuid.uuid4().hex}.sog'
                shutil.copy2(sog_source, pending_sog)
                pending_sog.replace(sog_result)
            from scan_transfer import gaussian_count
            state['gaussians'] = gaussian_count(sog_result, TRAINING['max_cap'])
            common.remove_training_plys(owned_path(output, state['ply']).parent)
            state["status"] = "complete"
            state["stage"] = "complete"
            state["spz"] = "result.spz"
            state['sog'] = 'result.sog'
            common.save_json(manifest, state)
            print(f"Finished: {sog_result}", flush=True)
        except BaseException as exc:
            state["status"] = "failed"
            state["error"] = str(exc)
            common.save_json(manifest, state)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Video file or flat, naturally ordered JPG/PNG folder")
    parser.add_argument("output", type=Path, help="New/empty job folder, normally /output/job-name")
    parser.add_argument("--max-images", type=int, default=MAX_IMAGES, help="Optional selection cap; otherwise use the repo defaults")
    parser.add_argument("--resume", action="store_true", help="Validate input/artifacts and skip completed stages")
    parser.add_argument("--check", action="store_true", help="Check versions and GPU without writing output")
    parser.add_argument("--select-only", action="store_true", help="Run CPU image selection only, then exit")
    parser.add_argument("--studio", type=Path, default=common.studio_path(), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.max_images is not None and args.max_images < 3:
        parser.error("--max-images must be at least 3 for reconstruction")
    try:
        run(args)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
