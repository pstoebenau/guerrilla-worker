"""Desktop: train an existing undistorted COLMAP dataset with the shared settings and export SPZ."""
import argparse
from pathlib import Path
import time

import pipeline_common as common
from pipeline_defaults import TRAINING

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, help='New folder (default: output/training-TIMESTAMP)')
    parser.add_argument('--studio', type=Path, default=common.studio_path())
    parser.add_argument('--max-cap', type=int, default=TRAINING['max_cap'])
    parser.add_argument('--mask-mode', choices=['none', 'segment', 'ignore', 'segment_and_ignore', 'alpha_consistent'], default='none')
    args = parser.parse_args()
    dataset = args.dataset.resolve()
    sparse = dataset / 'sparse'
    if not (sparse / 'cameras.bin').is_file():
        sparse = sparse / '0'
    if not (dataset / 'images').is_dir() or not all((sparse / name).is_file() for name in ['cameras.bin', 'images.bin', 'points3D.bin']):
        parser.error('Dataset must contain images and a COLMAP sparse model')
    if args.max_cap <= 0:
        parser.error('--max-cap must be positive')
    if args.mask_mode != 'none' and not (dataset / 'masks').is_dir():
        parser.error('Masked training requires a masks directory')
    output = args.output or ROOT / 'output' / ('training-' + time.strftime('%Y%m%d-%H%M%S'))
    if not (sparse / 'densification.json').exists():
        output.parent.mkdir(parents=True, exist_ok=True)
        common.densify(args.studio, dataset, output.with_name(output.name + '-densification'), args.max_cap)
    else:
        import json
        from pipeline_defaults import DENSIFICATION
        report = json.loads((sparse / 'densification.json').read_text(encoding='utf-8'))
        if (report['settings'] != dict(DENSIFICATION, max_points=args.max_cap)
                or common.file_hash(sparse / 'points3D.ply') != report['sha256']):
            raise ValueError('Dense initialization settings/content differ; use a fresh dataset')
    ply = common.train(args.studio, dataset, output, dict(TRAINING, max_cap=args.max_cap, mask_mode=args.mask_mode),
                       extra=['--mask-mode', args.mask_mode])
    sog = common.export_sog(args.studio, ply, output / 'result.sog', output / 'export-sog.log', args.max_cap)
    common.export_spz(args.studio, ply, output / 'result.spz', output / 'export-spz.log')
    common.remove_training_plys(output)
    print(sog)


if __name__ == '__main__':
    main()
