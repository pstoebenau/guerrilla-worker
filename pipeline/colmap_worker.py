"""Guerrilla reconstruction using the public pycolmap API, without Studio plugins."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import uuid

import pipeline_common as common
import scan_settings


def probe(settings=None):
    import pycolmap as pc
    settings = settings or scan_settings.defaults()['reconstruction']
    if pc.__version__.split('+')[0] != '4.0.2' or not pc.has_cuda:
        raise RuntimeError('Reconstruction requires CUDA-enabled pycolmap 4.0.2 in the worker Python')
    required = ['extract_features', 'match_exhaustive', 'undistort_images']
    required += (['global_mapping', 'GlobalPipelineOptions'] if settings['mode'] == 'global'
                 else ['incremental_mapping', 'IncrementalPipelineOptions'])
    if settings['mode'] == 'global' and settings['calibrate_intrinsics']:
        required.append('calibrate_view_graph')
    if any(not hasattr(pc, name) for name in required):
        raise RuntimeError('Installed pycolmap lacks required reconstruction APIs')
    return {'engine': 'guerrilla-pycolmap', 'pycolmap': pc.__version__, 'defaults': settings}


def mapping_options(pc, settings):
    if settings['mode'] == 'global':
        options = pc.GlobalPipelineOptions()
        options.mapper.ba_num_iterations = settings['ba_iterations']
        options.mapper.bundle_adjustment.ceres.use_gpu = True
    else:
        options = pc.IncrementalPipelineOptions()
        options.ba_global_max_num_iterations = settings['ba_iterations']
        options.ba_use_gpu = True
    options.random_seed = 0
    return options


def reconstruct(images, output, settings=None, masks=None, retain_artifacts=True):
    import pycolmap as pc
    settings = settings or scan_settings.defaults()['reconstruction']
    identity = probe(settings)
    images, output = Path(images).resolve(), Path(output).resolve()
    if images == output or images.is_relative_to(output):
        raise ValueError('Output must not contain source images')
    paths = common.selected_images(images)
    names = [path.name for path in paths]
    mask_signature = None
    if masks:
        from alignment_masks import mask_files
        masks = Path(masks).resolve()
        mask_signature = common.fingerprint(mask_files(paths, masks))
    signature = {'images': str(images), 'input_sha256': common.fingerprint(paths),
                 'settings': settings, 'mask_sha256': mask_signature, **identity}
    output.mkdir(parents=True, exist_ok=True)
    checkpoint = output / 'colmap-state.json'
    if checkpoint.exists():
        state = json.loads(checkpoint.read_text())
        if state['signature'] != signature:
            raise ValueError('Reconstruction inputs or settings changed; use a new output folder')
        workspace = output / state['workspace']
        if workspace.resolve().parent != output or workspace.is_symlink() or not workspace.is_dir():
            raise ValueError('Invalid reconstruction workspace')
    else:
        workspace = output / ('colmap-work-' + uuid.uuid4().hex)
        workspace.mkdir()
        state = {'signature': signature, 'workspace': workspace.name, 'completed': []}
        common.save_json(checkpoint, state)
    database = workspace / 'database.db'

    def complete(stage):
        state['completed'].append(stage)
        common.save_json(checkpoint, state)

    if 'features' not in state['completed']:
        reader = pc.ImageReaderOptions(camera_model='OPENCV')
        if masks:
            reader.mask_path = str(masks)
        extraction = pc.FeatureExtractionOptions()
        extraction.type = pc.FeatureExtractorType.SIFT
        extraction.sift.max_num_features = settings['features']
        extraction.use_gpu = True
        extraction.gpu_index = '0'
        print('COLMAP: extracting SIFT features on GPU', flush=True)
        pc.extract_features(database_path=database, image_path=images, image_names=names,
                            camera_mode=pc.CameraMode.SINGLE, reader_options=reader,
                            extraction_options=extraction)
        complete('features')
    if masks:
        # Preserve the audit interface used by the runner without patching pycolmap.
        from contextlib import closing
        import sqlite3
        from alignment_masks import audit_features
        with closing(sqlite3.connect(database)) as source:
            with closing(sqlite3.connect(output / 'masked-features.db')) as target:
                source.backup(target)
        common.save_json(output / 'mask-feature-audit.json', audit_features(database, masks, paths))
    if 'matches' not in state['completed']:
        matching = pc.FeatureMatchingOptions()
        matching.use_gpu = True
        matching.gpu_index = '0'
        matching.max_num_matches = settings['matches']
        print('COLMAP: exhaustive feature matching on GPU', flush=True)
        pc.match_exhaustive(database_path=database, matching_options=matching,
                            pairing_options=pc.ExhaustivePairingOptions(block_size=15))
        complete('matches')
    if settings['mode'] == 'global' and settings['calibrate_intrinsics'] and 'calibration' not in state['completed']:
        calibration = pc.ViewGraphCalibrationOptions()
        calibration.min_calibrated_pair_ratio = 0.1
        print('COLMAP: calibrating camera intrinsics', flush=True)
        pc.calibrate_view_graph(database, options=calibration)
        complete('calibration')
    # Every mapping attempt is isolated; completed feature and match databases survive.
    attempt = workspace / ('mapping-' + uuid.uuid4().hex)
    attempt.mkdir()
    mapping = pc.global_mapping if settings['mode'] == 'global' else pc.incremental_mapping
    print(f"COLMAP: {settings['mode']} mapping", flush=True)
    models = mapping(database_path=database, image_path=images, output_path=attempt,
                     options=mapping_options(pc, settings))
    if not models:
        raise RuntimeError('COLMAP produced no reconstruction; inspect capture overlap')
    best_id = max(models, key=lambda key: models[key].num_reg_images())
    model = models[best_id]
    ba = pc.BundleAdjustmentOptions()
    ba.ceres.use_gpu = True
    ba.ceres.solver_options.max_num_iterations = settings['ba_iterations'] * 2
    ba.ceres.solver_options.function_tolerance = 1e-8
    pc.bundle_adjustment(model, ba)
    registered = model.num_reg_images()
    if registered < 3 or model.num_points3D() == 0:
        raise RuntimeError('Reconstruction needs at least three registered images and nonempty geometry')
    sparse = attempt / 'sparse'
    sparse.mkdir()
    model.write(sparse)
    dataset = attempt / 'dataset'
    print('COLMAP: undistorting images for training', flush=True)
    pc.undistort_images(output_path=dataset, input_path=sparse, image_path=images,
                        output_type='COLMAP', copy_policy=pc.FileCopyType.copy,
                        undistort_options=pc.UndistortCameraOptions())
    refined_sparse = dataset / 'sparse'
    if not (refined_sparse / 'cameras.bin').exists():
        refined_sparse /= '0'
    refined = {'enabled': False}
    if settings['refine_cameras']:
        from camera_refinement import refine_dataset
        refined = refine_dataset(refined_sparse, attempt / 'camera-refinement.json')
    unregistered = sorted(set(names) - {im.name for im in model.images.values()})
    result = {**identity, 'success': True, 'dataset_dir': str(dataset), 'recon_dir': str(refined_sparse),
              'dataset_relative': dataset.relative_to(output).as_posix(),
              'input_images': len(names), 'registered_images': registered,
              'registered_fraction': registered / len(names), 'points3D': model.num_points3D(),
              'mean_reprojection_error': model.compute_mean_reprojection_error(),
              'unregistered_images': unregistered, 'camera_refinement': refined,
              'model_count': len(models), 'selected_model_id': int(best_id), 'warnings': []}
    if unregistered:
        result['warnings'].append(f'Only {registered}/{len(names)} selected images registered; scene may be incomplete.')
    common.save_json(output / 'colmap-result.json', result)
    if not retain_artifacts:
        # Desktop runs historically discard the feature/match database after
        # successful publication. Platform runs retain it for archival.
        # Reset the checkpoint before cleanup so an interrupted cleanup is safe.
        state['completed'] = []
        common.save_json(checkpoint, state)
        for name in ('database.db', 'database.db-wal', 'database.db-shm'):
            (workspace / name).unlink(missing_ok=True)
    for warning in result['warnings']:
        print(f'Warning: {warning}', flush=True)
    print(f'COLMAP complete: {registered}/{len(names)} images, {model.num_points3D()} points', flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('images', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    reconstruct(args.images, args.output)
