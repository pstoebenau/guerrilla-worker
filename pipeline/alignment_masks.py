"""COLMAP feature masks kept separate from full-image splat training."""
from pathlib import Path
from contextlib import closing
import sqlite3


def mask_files(images, folder):
    paths = [Path(folder) / (image.name + '.png') for image in images]
    if any(not p.is_file() or p.is_symlink() for p in paths):
        raise ValueError('Each selected image requires a COLMAP mask named <image filename>.png')
    return paths


def install_feature_masks(pycolmap, folder, database_copy):
    """Wrap this worker's extraction call, preserving the installed plugin on disk."""
    original = pycolmap.extract_features

    def extract(*args, **kwargs):
        kwargs['reader_options'].mask_path = str(Path(folder).resolve())
        result = original(*args, **kwargs)
        # The plugin discards its staging database after publishing the dataset.
        with closing(sqlite3.connect(kwargs['database_path'])) as source:
            with closing(sqlite3.connect(database_copy)) as destination:
                source.backup(destination)
        return result

    pycolmap.extract_features = extract


def audit_features(database, folder, images):
    """Verify native feature extraction excluded every masked keypoint."""
    import cv2
    import numpy as np
    paths = {image.name: image for image in images}
    records = []
    with closing(sqlite3.connect(f'{Path(database).resolve().as_uri()}?mode=ro', uri=True)) as connection:
        rows = connection.execute('SELECT images.name, keypoints.rows, keypoints.cols, keypoints.data '
                                  'FROM images JOIN keypoints USING(image_id)').fetchall()
    if {r[0] for r in rows} != set(paths):
        raise ValueError('Feature database does not contain the complete reviewed selection')
    for name, count, columns, data in rows:
        mask = cv2.imread(str(Path(folder)/(name+'.png')), cv2.IMREAD_GRAYSCALE)
        image = cv2.imread(str(paths[name]), cv2.IMREAD_UNCHANGED)
        if mask is None or image is None or mask.shape != image.shape[:2]:
            raise ValueError(f'Mask/image dimensions differ: {name}')
        if not np.all((mask == 0) | (mask == 255)) or not (mask == 0).any() or not (mask == 255).any():
            raise ValueError(f'Mask must contain both excluded black and retained white pixels: {name}')
        points = np.frombuffer(data, dtype=np.float32).reshape(count, columns)[:, :2]
        # COLMAP's bitmap coordinates use pixel centers at half-integers.
        xy = np.floor(points).astype(int)
        h, w = mask.shape
        if ((xy < 0).any() or (xy[:, 0] >= w).any() or (xy[:, 1] >= h).any()):
            raise ValueError(f'Out-of-bounds features: {name}')
        excluded = int(np.count_nonzero(mask[xy[:, 1], xy[:, 0]] == 0))
        records.append({'image': name, 'features': count, 'masked_features': excluded})
    bad = sum(r['masked_features'] for r in records)
    if bad:
        raise ValueError(f'COLMAP retained {bad} features inside exclusion masks')
    return {'images': len(records), 'features': sum(r['features'] for r in records),
            'masked_features': bad, 'records': records}


def unmasked_training(dataset, config):
    if (Path(dataset)/'masks').exists():
        raise ValueError('Alignment-only masks must not be published into the training dataset')
    return dict(config, mask_mode='none'), ['--mask-mode', 'none']
