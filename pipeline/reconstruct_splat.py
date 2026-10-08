"""Desktop: run the installed LichtFeld COLMAP plugin, train, and export SPZ.

Run with the workspace Python; the COLMAP worker re-invokes this file with
Studio's bundled Python and the installed plugin's dependencies.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

import pipeline_common as common
import scan_settings
from pipeline_defaults import TRAINING, EXPORT_FORMAT


def load_plugin(plugin):
    import site
    site.addsitedir(str(plugin / ".venv/Lib/site-packages"))
    import sys
    sys.path.insert(0, str(plugin.parent))
    import importlib
    return importlib.import_module(plugin.name + ".panels.main_panel")


def worker(args):
    """Runs under Studio's bundled Python: COLMAP via the plugin, then camera refinement."""
    module = load_plugin(args.plugin)
    reconstruction = args.options['reconstruction']
    params = module.ColmapParams(**{**module.PRESET_NORMAL_PARAMS, "sift_max_num_features": reconstruction['features'],
                                    "sift_max_num_matches": reconstruction['matches'],
                                    "ba_global_max_num_iterations": reconstruction['ba_iterations']})
    params.reconstruction_mode = args.reconstruction_mode
    params.use_view_graph_calibration = reconstruction['calibrate_intrinsics']
    if args.reconstruction_mode == "global" and not all(
            hasattr(module.pycolmap, name) for name in ("global_mapping", "GlobalPipelineOptions")):
        raise RuntimeError("Installed pycolmap does not support GLOMAP global mapping")
    if args.reconstruction_mode == 'global' and reconstruction['calibrate_intrinsics'] and not hasattr(module.pycolmap, 'calibrate_view_graph'):
        raise RuntimeError('Installed pycolmap does not support intrinsic calibration')
    if args.worker == "probe":
        print(json.dumps({"plugin": str(args.plugin), "defaults": dataclasses.asdict(params),
                          "pycolmap": module.pycolmap.__version__}), flush=True)
        return

    output = args.output.resolve()
    if getattr(args, 'retain_artifacts', False):
        class RetainingShutil:
            def __getattr__(self, name):
                return getattr(shutil, name)

            def rmtree(self, path, *unused, **options):
                target = Path(path).resolve()
                if not target.exists() and options.get('ignore_errors'):
                    return
                if target == output or not target.is_relative_to(output):
                    raise ValueError('Plugin cleanup path escapes its reconstruction directory')
                retained = output / 'retained-intermediates' / uuid.uuid4().hex / target.name
                retained.parent.mkdir(parents=True)
                shutil.move(str(target), str(retained))
        # The plugin normally deletes its feature/match workspace on success.
        # Preserve those files without changing reconstruction itself.
        module.shutil = RetainingShutil()
    # The plugin resets directories and replaces images while publishing.
    # Give it only a fresh, script-owned staging dataset beneath output.
    dataset = output / "dataset"
    images = dataset / "images"
    if dataset.exists():
        raise FileExistsError(f"Refusing to reconstruct over {dataset}")
    if dataset.resolve().parent != output or images.resolve().parent != dataset:
        raise ValueError("Dataset paths escape the output folder")
    paths = common.selected_images(args.images)
    images.mkdir(parents=True)
    for path in paths:
        shutil.copy2(path, images / path.name)

    class LoggedJob(module.ColmapReconJob):
        def _append_log(self, message):
            super()._append_log(message)
            print(message, flush=True)

    original_mapping = module.pycolmap.global_mapping

    def adjusted_mapping(*mapping_args, **kwargs):
        models = original_mapping(*mapping_args, **kwargs)
        for model in models.values():
            options = module.pycolmap.BundleAdjustmentOptions()
            options.ceres.use_gpu = True
            options.ceres.solver_options.max_num_iterations = reconstruction['ba_iterations'] * 2
            options.ceres.solver_options.function_tolerance = 1e-8
            module.pycolmap.bundle_adjustment(model, options)
        return models

    if args.reconstruction_mode == "global":
        module.pycolmap.global_mapping = adjusted_mapping
    if args.colmap_masks:
        from alignment_masks import install_feature_masks
        install_feature_masks(module.pycolmap, args.colmap_masks, output/'masked-features.db')
    job = LoggedJob(str(images), params)
    job.start()
    job._thread.join()
    result = job.result
    if result is None or not result.success:
        raise RuntimeError(result.error if result else "Plugin returned no result")
    from camera_refinement import refine_dataset
    calibration = (refine_dataset(result.recon_dir, output / "camera-refinement.json")
                   if reconstruction['refine_cameras'] else {'enabled': False})
    reconstruction = module.pycolmap.Reconstruction(result.recon_dir)
    registered = reconstruction.num_reg_images()
    if registered < 3 or reconstruction.num_points3D() == 0:
        raise RuntimeError("Reconstruction has insufficient registered images/points")
    common.save_json(output / "colmap-result.json", {
        **dataclasses.asdict(result),
        "defaults": dataclasses.asdict(params),
        "camera_refinement": calibration,
        "input_images": len(paths),
        "registered_images": registered,
        "unregistered_images": sorted({p.name for p in paths} -
                                      {im.name for im in reconstruction.images.values()}),
    })
    print(f"COLMAP complete: {registered}/{len(paths)} images registered", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, help='Per-scan options JSON')
    parser.add_argument('--retain-artifacts', action='store_true', help='Retain plugin workspaces for platform archival')
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--studio", type=Path, default=common.studio_path())
    parser.add_argument("--plugin", type=Path, default=common.plugin_path())
    parser.add_argument("--check", action="store_true",
                        help="Check the selected images and plugin without reconstruction")
    parser.add_argument("--resume", action="store_true",
                        help="Skip completed stages in this script's existing output")
    parser.add_argument("--reconstruction-mode", choices=["incremental", "global"],
                        default=None, help="Use global for the plugin's GLOMAP option")
    parser.add_argument("--reconstruction-only", action="store_true",
                        help="Stop after reconstruction and image undistortion")
    parser.add_argument("--colmap-masks", type=Path,
                        help="COLMAP feature masks only; training uses full unmasked images")
    parser.add_argument("--max-cap", type=int)
    parser.add_argument("--worker", choices=["probe", "colmap"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    # Worker processes use Studio's bundled Python. The parent validates the
    # immutable resolved settings before launching them.
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
    args.plugin = args.plugin.resolve()
    args.studio = args.studio.resolve()
    if args.colmap_masks:
        args.colmap_masks = args.colmap_masks.resolve()
    if args.worker:
        worker(args)
        return
    python = args.studio.parent / "python.exe" if os.name == 'nt' else Path(sys.executable)
    for path in (args.studio, python, args.plugin / "panels/main_panel.py"):
        if not path.is_file():
            raise FileNotFoundError(path)
    paths = common.selected_images(args.images)
    if args.images == args.output or args.images.is_relative_to(args.output):
        raise ValueError("Output must not contain the source images")
    # Studio's bundled Python does not add a script's directory to sys.path.
    # Pass this project's directory explicitly before importing the worker.
    bootstrap = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                 "from reconstruct_splat import main; main()")
    command = [python, "-u", "-c", bootstrap, Path(__file__).resolve().parent, "--plugin", args.plugin,
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
                 "studio": str(args.studio), "plugin": str(args.plugin), "completed": [],
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
