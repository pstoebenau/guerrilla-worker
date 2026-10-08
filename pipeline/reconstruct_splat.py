"""Reconstruct with pycolmap, then densify, train and export with LichtFeld."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

import pipeline_common as common
import scan_settings
from pipeline_defaults import TRAINING, EXPORT_FORMAT


def worker(args):
    """Run Guerrilla's reconstruction directly through pycolmap."""
    from colmap_worker import probe, reconstruct
    report = probe(args.options['reconstruction'])
    if args.worker == 'probe':
        print(json.dumps(report), flush=True)
        return
    reconstruct(args.images, args.output, args.options['reconstruction'],
                masks=args.colmap_masks, retain_artifacts=args.retain_artifacts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, help='Per-scan options JSON')
    parser.add_argument('--retain-artifacts', action='store_true', help='Retain feature/match workspaces for platform archival')
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--studio", type=Path, default=common.studio_path())
    parser.add_argument("--check", action="store_true",
                        help="Check the selected images and pycolmap without reconstruction")
    parser.add_argument("--resume", action="store_true",
                        help="Skip completed stages in this script's existing output")
    parser.add_argument("--reconstruction-mode", choices=["incremental", "global"],
                        default=None, help="Use global for GLOMAP")
    parser.add_argument("--reconstruction-only", action="store_true",
                        help="Stop after reconstruction and image undistortion")
    parser.add_argument("--colmap-masks", type=Path,
                        help="COLMAP feature masks only; training uses full unmasked images")
    parser.add_argument("--max-cap", type=int)
    parser.add_argument("--worker", choices=["probe", "colmap"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    # The parent validates immutable settings before launching the worker.
    args.options = scan_settings.load(args.settings, validate=not bool(args.worker))
    args.max_cap = args.max_cap if args.max_cap is not None else args.options['training']['max_cap']
    args.reconstruction_mode = args.reconstruction_mode or args.options['reconstruction']['mode']
    args.options['reconstruction']['mode'] = args.reconstruction_mode
    training_config = scan_settings.training(args.options, args.max_cap)
    dense_config = scan_settings.densification(args.options)
    calibration_enabled = args.options['reconstruction']['calibrate_intrinsics']
    if args.max_cap <= 0:
        parser.error("--max-cap must be positive")
    args.images = args.images.resolve()
    args.output = args.output.resolve()
    args.studio = args.studio.resolve()
    if args.colmap_masks:
        args.colmap_masks = args.colmap_masks.resolve()
    if args.worker:
        worker(args)
        return
    python = Path(sys.executable)
    for path in ((python,) if args.reconstruction_only else (args.studio, python)):
        if not path.is_file():
            raise FileNotFoundError(path)
    paths = common.selected_images(args.images)
    if args.images == args.output or args.images.is_relative_to(args.output):
        raise ValueError("Output must not contain the source images")
    # Use this worker's Python and project code, independent of Studio's UI.
    bootstrap = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                 "from reconstruct_splat import main; main()")
    command = [python, "-u", "-c", bootstrap, Path(__file__).resolve().parent,
               "--images", args.images, "--output", args.output,
               "--reconstruction-mode", args.reconstruction_mode]
    if args.colmap_masks:
        command += ['--colmap-masks', args.colmap_masks]
    if args.settings:
        command += ['--settings', args.settings.resolve()]
    if args.retain_artifacts:
        command += ['--retain-artifacts']
    subprocess.run([str(p) for p in command + ["--worker", "probe"]], check=True)
    print(f"Selected images: {len(paths)}; output: {args.output}", flush=True)
    if args.check:
        return
    signature = common.fingerprint(paths)
    mask_signature = None
    if args.colmap_masks:
        from alignment_masks import mask_files
        mask_signature = common.fingerprint(mask_files(paths, args.colmap_masks))
    manifest_path = args.output / "pipeline.json"
    if args.resume:
        state = json.loads(manifest_path.read_text(encoding="utf-8"))
        if state["input_sha256"] != signature or state["images"] != str(args.images):
            raise ValueError("Source images changed; use a new output directory")
        if state.get("reconstruction_mode", "incremental") != args.reconstruction_mode:
            raise ValueError("Reconstruction mode changed; use a new output directory")
        if state.get("max_cap", TRAINING["max_cap"]) != args.max_cap:
            raise ValueError("Gaussian cap changed; use a new output directory")
        if state.get('colmap_masks_sha256') != mask_signature:
            raise ValueError('Alignment masks changed; use a new output directory')
        if state.get('training') != training_config or state.get('densification_settings') != dense_config:
            raise ValueError('Training or densification settings changed; use a new output directory')
        if state.get('export_format') != EXPORT_FORMAT or state.get('calibrate_intrinsics') != calibration_enabled:
            raise ValueError('Export or calibration settings changed; use a new output directory')
        legacy = scan_settings.defaults()['reconstruction']
        legacy['mode'] = state.get('reconstruction_mode', 'incremental')
        legacy['calibrate_intrinsics'] = state.get('calibrate_intrinsics', True)
        if state.get('options', {'reconstruction': legacy})['reconstruction'] != args.options['reconstruction']:
            raise ValueError('Scan settings changed; use a new output directory')
    else:
        args.output.mkdir(parents=True, exist_ok=False)
        state = {"images": str(args.images), "input_count": len(paths), "input_sha256": signature,
                 "studio": str(args.studio), "reconstruction_engine": "guerrilla-pycolmap", "completed": [],
                 "max_cap": args.max_cap, "reconstruction_mode": args.reconstruction_mode,
                 "training": training_config, 'options': args.options,
                 "densification_settings": dense_config, "calibrate_intrinsics": calibration_enabled,
                 "export_format": EXPORT_FORMAT,
                 "colmap_masks_sha256": mask_signature,
                 "colmap_masks": str(args.colmap_masks) if args.colmap_masks else None}
    common.save_json(manifest_path, state)
    try:
        if "colmap" not in state["completed"]:
            common.run_logged(command + ["--worker", "colmap"], args.output / "colmap.log")
            result = json.loads((args.output / "colmap-result.json").read_text())
            state["dataset"] = result["dataset_dir"]
            state["registered_images"] = result["registered_images"]
            if args.colmap_masks:
                from alignment_masks import audit_features
                audit = audit_features(args.output/'masked-features.db', args.colmap_masks, paths)
                common.save_json(args.output/'mask-feature-audit.json', audit)
            state["completed"].append("colmap")
            common.save_json(manifest_path, state)
        if args.reconstruction_only:
            state.pop("error", None)
            common.save_json(manifest_path, state)
            print(f"Reconstruction finished: {state['dataset']}", flush=True)
            return
        if 'densify' not in state['completed']:
            if 'densification_attempt' not in state:
                state['densification_attempt'] = str(args.output / ('densification-' + time.strftime('%Y%m%d-%H%M%S')))
                common.save_json(manifest_path, state)
            attempt = Path(state['densification_attempt'])
            state['densification'] = common.densify(args.studio, state['dataset'], attempt, args.max_cap,
                                                   settings=dense_config)
            state['completed'].append('densify')
            common.save_json(manifest_path, state)
        dense = state['densification']
        if common.file_hash(dense['pointcloud']) != dense['sha256']:
            raise ValueError('Dense initialization changed; refusing to resume')
        if "train" not in state["completed"]:
            training = args.output / ("training-" + time.strftime("%Y%m%d-%H%M%S"))
            config, extra = training_config.copy(), []
            if args.colmap_masks:
                from alignment_masks import unmasked_training
                config, extra = unmasked_training(state['dataset'], config)
            ply = common.train(args.studio, state["dataset"], training, config, extra=extra)
            state["ply"] = str(ply)
            state["completed"].append("train")
            common.save_json(manifest_path, state)
        if "export" not in state["completed"]:
            sog = common.export_sog(args.studio, state['ply'], args.output / 'result.sog',
                                    args.output / 'export-sog.log', args.max_cap)
            spz = args.output / 'result.spz'
            if not spz.exists():
                common.export_spz(args.studio, state["ply"], spz, args.output / "export-spz.log")
            if not spz.is_file() or spz.stat().st_size < 100:
                raise ValueError('SPZ export is missing or empty')
            from scan_transfer import gaussian_count
            state['sog'], state['gaussians'] = str(sog), gaussian_count(sog, args.max_cap)
            state["spz"] = str(spz)
            state["completed"].append("export")
            common.save_json(manifest_path, state)
        common.remove_training_plys(Path(state['ply']).parent)
        state.pop("error", None)
        common.save_json(manifest_path, state)
        print(f"Finished: {state['sog']}", flush=True)
    except BaseException as exc:
        state["error"] = str(exc)
        common.save_json(manifest_path, state)
        raise


if __name__ == "__main__":
    main()
