"""Tested GLOMAP setup with validated per-frame calibration refinement."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline_common import selected_images
from pipeline_defaults import FEATURES, MATCHES, BA_ITERATIONS, CALIBRATE_INTRINSICS


TESTED_PRESET = {
    "camera_model": "OPENCV", "single_camera": True, "downsample_multiplier": 1,
    "sift_max_num_features": FEATURES, "matcher": "exhaustive",
    "reconstruction_mode": "global", "use_view_graph_calibration": CALIBRATE_INTRINSICS,
    "sift_max_num_matches": MATCHES, "exhaustive_block_size": 15,
    "ba_global_max_num_iterations": BA_ITERATIONS,
    "per_frame_calibration": "validation-gated",
}


def reconstruct(images, output):
    import pycolmap as pc

    if pc.__version__ != "4.0.2" or not pc.has_cuda:
        raise RuntimeError("This pipeline requires pycolmap-cuda12==4.0.2")
    names = [p.name for p in selected_images(images)]
    output.mkdir(parents=True, exist_ok=False)
    database = output / "database.db"
    mapping = output / "mapping"
    mapping.mkdir()
    reader = pc.ImageReaderOptions(camera_model="OPENCV")
    extraction = pc.FeatureExtractionOptions()
    extraction.type = pc.FeatureExtractorType.SIFT
    extraction.sift.max_num_features = FEATURES
    extraction.use_gpu = True
    extraction.gpu_index = "0"
    print("COLMAP: extracting SIFT features on GPU", flush=True)
    pc.extract_features(database_path=database, image_path=images,
                        image_names=names,
                        camera_mode=pc.CameraMode.SINGLE, reader_options=reader,
                        extraction_options=extraction)
    matching = pc.FeatureMatchingOptions()
    matching.use_gpu = True
    matching.gpu_index = "0"
    matching.max_num_matches = MATCHES
    pairing = pc.ExhaustivePairingOptions(block_size=15)
    print("COLMAP: exhaustive feature matching on GPU", flush=True)
    pc.match_exhaustive(database_path=database, matching_options=matching, pairing_options=pairing)
    if CALIBRATE_INTRINSICS:
        if not hasattr(pc, 'calibrate_view_graph'):
            raise RuntimeError('pycolmap does not support intrinsic calibration')
        calibration = pc.ViewGraphCalibrationOptions()
        calibration.min_calibrated_pair_ratio = 0.1
        print('COLMAP: calibrating camera intrinsics', flush=True)
        pc.calibrate_view_graph(database, options=calibration)
    options = pc.GlobalPipelineOptions()
    options.random_seed = 0
    options.mapper.ba_num_iterations = BA_ITERATIONS
    options.mapper.bundle_adjustment.ceres.use_gpu = True
    print("COLMAP: GLOMAP mapping", flush=True)
    models = pc.global_mapping(database_path=database, image_path=images,
                               output_path=mapping, options=options)
    if not models:
        raise RuntimeError("COLMAP produced no reconstruction; inspect selection and capture overlap")
    best_id = max(models, key=lambda key: models[key].num_reg_images())
    model = models[best_id]
    ba = pc.BundleAdjustmentOptions()
    ba.ceres.use_gpu = True
    ba.ceres.solver_options.max_num_iterations = 200
    ba.ceres.solver_options.function_tolerance = 1e-8
    pc.bundle_adjustment(model, ba)
    registered = model.num_reg_images()
    if registered < 3 or model.num_points3D() == 0:
        raise RuntimeError("Reconstruction needs at least three registered images and nonempty geometry")
    sparse = output / "sparse"
    sparse.mkdir()
    model.write(sparse)
    unregistered = sorted(set(names) - {im.name for im in model.images.values()})
    result = {"input_images": len(names), "registered_images": registered,
              "registered_fraction": registered / len(names),
              "points3D": model.num_points3D(), "mean_reprojection_error": model.compute_mean_reprojection_error(),
              "unregistered_images": unregistered, "preset": TESTED_PRESET,
              "model_count": len(models), "selected_model_id": best_id,
              "pycolmap": pc.__version__, "warnings": []}
    if unregistered:
        result["warnings"].append(f"Only {registered}/{len(names)} selected images registered; scene may be incomplete.")
    dataset = output / "dataset"
    print("COLMAP: undistorting images for training", flush=True)
    pc.undistort_images(output_path=dataset, input_path=sparse, image_path=images,
                        output_type="COLMAP", copy_policy=pc.FileCopyType.copy,
                        undistort_options=pc.UndistortCameraOptions())
    from camera_refinement import refine_dataset
    refined_sparse = dataset / "sparse"
    if not (refined_sparse / "cameras.bin").exists():
        refined_sparse = refined_sparse / "0"
    result['camera_refinement'] = refine_dataset(refined_sparse, output / 'camera-refinement.json')
    (output / "colmap-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    for warning in result["warnings"]:
        print(f"Warning: {warning}", flush=True)
    print(f"COLMAP complete: {registered}/{len(names)} images, {model.num_points3D()} points", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    reconstruct(args.images, args.output)
