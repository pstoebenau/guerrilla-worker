"""Desktop entry point: download/select a video, reconstruct, train, export."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import urllib.parse

import pipeline_common as common
import scan_settings
from pipeline_defaults import EXPORT_FORMAT
from scan_transfer import download_video, gaussian_count, spz_count

ROOT = Path(__file__).resolve().parent
SCANS_ROOT = Path.cwd() / 'scans'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", help="Local video, direct video URL, or shared Google Photos video link")
    parser.add_argument('--settings', type=Path, help='Per-scan options JSON (scan-settings.schema.json)')
    parser.add_argument("--output", type=Path,
                        help="New job folder (default: <video-stem>-splat beside the video)")
    parser.add_argument("--studio", type=Path, help="Override the LichtFeld Studio executable")
    parser.add_argument("--max-images", type=int, help="Override the shared frame selection default")
    parser.add_argument("--selected-images", type=Path, help="Reuse an already reviewed selection folder")
    parser.add_argument("--colmap-masks", type=Path, help="Feature exclusion masks for alignment only; never used for training")
    parser.add_argument("--reconstruction-mode", choices=['incremental', 'global'],
                        default=None, help="incremental: COLMAP; global: GLOMAP")
    parser.add_argument("--max-cap", type=int, help="Maximum Gaussians")
    parser.add_argument("--resume", action="store_true", help="Resume this job with the same arguments")
    parser.add_argument("--select-only", action="store_true",
                        help="Stop after frame selection for review; continue with --resume")
    args = parser.parse_args(argv)
    try:
        options = scan_settings.load(args.settings)
        args.max_cap = args.max_cap if args.max_cap is not None else options['training']['max_cap']
        args.reconstruction_mode = args.reconstruction_mode or options['reconstruction']['mode']
        args.max_images = args.max_images if args.max_images is not None else options['selection']['max_images']
        options['training']['max_cap'] = args.max_cap
        options['reconstruction']['mode'] = args.reconstruction_mode
        options['selection']['max_images'] = args.max_images
        if options['selection']['preserve_coverage'] and args.max_images is not None:
            raise ValueError('Preserve coverage cannot be combined with a maximum frame count')
    except Exception as exc:
        parser.error(str(exc))
    remote = urllib.parse.urlsplit(args.video).scheme in {'http', 'https'}
    source = None if remote else Path(args.video).resolve()
    default_output = (SCANS_ROOT / ('video-' + hashlib.sha256(args.video.encode()).hexdigest()[:12])
                      if remote else source.with_name(source.stem + '-splat'))
    output = (args.output or default_output).resolve()
    if not remote and not source.is_file():
        parser.error(f"Input video does not exist: {source}")
    if not remote and (source == output or source.is_relative_to(output)):
        parser.error("Output must not contain the input video")
    if output.exists() and not args.resume:
        parser.error("Output must be a new folder; choose another --output")
    if args.max_images is not None and args.max_images < 3:
        parser.error("--max-images must be at least 3")
    if args.max_cap <= 0:
        parser.error("--max-cap must be positive")
    settings = dict(video=args.video if remote else str(source), max_images=args.max_images,
                    max_cap=args.max_cap, studio=str((args.studio or common.studio_path()).resolve()),
                    reconstruction_mode=args.reconstruction_mode,
                    calibrate_intrinsics=options['reconstruction']['calibrate_intrinsics'],
                    densification=scan_settings.densification(options),
                    export_format=EXPORT_FORMAT,
                    selected_images=str(args.selected_images.resolve()) if args.selected_images else None,
                    colmap_masks=str(args.colmap_masks.resolve()) if args.colmap_masks else None,
                    training=scan_settings.training(options, args.max_cap), options=options)
    manifest = output / 'scan.json'

    def unused_folder(name):
        folder = output / name
        index = 2
        while folder.exists():
            folder = output / f'{name}-{index}'
            index += 1
        return folder

    try:
        if args.resume:
            state = json.loads(manifest.read_text())
            # The old plugin path is no longer a reconstruction setting.
            state['settings'].pop('plugin', None)
            state['settings'].setdefault('reconstruction_mode', 'global')
            state['settings'].setdefault('selected_images', None)
            state['settings'].setdefault('colmap_masks', None)
            if 'options' not in state['settings']:
                legacy = scan_settings.defaults()
                legacy['training']['max_cap'] = state['settings']['max_cap']
                legacy['selection']['max_images'] = state['settings']['max_images']
                legacy['reconstruction']['mode'] = state['settings']['reconstruction_mode']
                state['settings']['options'] = legacy
            if state['settings'] != settings:
                raise ValueError('Source or training settings changed; use a new output folder')
        else:
            output.mkdir(parents=True)
            state = {'settings': settings, 'completed': []}
            common.save_json(manifest, state)
        resolved_settings = output / 'settings.json'
        common.save_json(resolved_settings, options)
        if remote:
            source = output / 'source.mp4'
            if 'download' not in state['completed']:
                print('Downloading video...', flush=True)
                # A previous attempt may have finished the atomic rename before saving state.
                if not source.exists():
                    download_video(args.video, source)
                state['completed'].append('download')
        signature = common.file_hash(source)
        if state.get('source_sha256', signature) != signature:
            raise ValueError('Source video changed; use a new output folder')
        state['source_sha256'] = signature
        common.save_json(manifest, state)
        if args.selected_images:
            selection = args.selected_images.resolve()
            selection_signature = common.fingerprint(common.selected_images(selection))
            if state.get('selection_sha256', selection_signature) != selection_signature:
                raise ValueError('Reviewed selection changed; use a new output folder')
            state['selection_sha256'] = selection_signature
            state['selection'] = str(selection)
            if 'selection' not in state['completed']:
                state['completed'].append('selection')
            common.save_json(manifest, state)
        if 'selection' not in state['completed']:
            selection = unused_folder('selected')
            command = [sys.executable, '-u', ROOT/'select_frames.py', source, selection]
            command += scan_settings.selection_arguments(options)
            print('Selecting video frames; details in selection.log', flush=True)
            common.run_logged(command, output/'selection.log')
            state['selection'] = str(selection)
            state['completed'].append('selection')
            common.save_json(manifest, state)
        if args.select_only:
            print(f"Selection ready for review: {state['selection']}", flush=True)
            return 0
        if args.colmap_masks:
            from alignment_masks import mask_files
            masks = mask_files(common.selected_images(state['selection']), args.colmap_masks)
            mask_signature = common.fingerprint(masks)
            if state.get('colmap_masks_sha256', mask_signature) != mask_signature:
                raise ValueError('Alignment masks changed; use a new output folder')
            state['colmap_masks_sha256'] = mask_signature
            common.save_json(manifest, state)
        if 'reconstruction' not in state['completed']:
            reconstruction = Path(state.get('reconstruction', output/'reconstruction'))
            resume_reconstruction = False
            if (reconstruction/'pipeline.json').exists():
                previous = json.loads((reconstruction/'pipeline.json').read_text())
                resume_reconstruction = 'colmap' in previous['completed']
            if reconstruction.exists() and not resume_reconstruction:
                reconstruction = unused_folder('reconstruction')
            state['reconstruction'] = str(reconstruction)
            common.save_json(manifest, state)
            command = [sys.executable, '-u', ROOT/'reconstruct_splat.py',
                       '--images', state['selection'], '--output', reconstruction,
                       '--max-cap', args.max_cap, '--studio', settings['studio'],
                       '--reconstruction-mode', args.reconstruction_mode,
                       '--settings', resolved_settings]
            if args.colmap_masks:
                command += ['--colmap-masks', args.colmap_masks.resolve()]
            if resume_reconstruction:
                command.append('--resume')
            print('Reconstructing, training, and exporting; details in reconstruction.log', flush=True)
            common.run_logged(command, output/'reconstruction.log')
            state['completed'].append('reconstruction')
            common.save_json(manifest, state)
        reconstruction = Path(state['reconstruction'])
        result = json.loads((reconstruction/'pipeline.json').read_text())
        sog, spz = Path(result['sog']), Path(result['spz'])
        count = gaussian_count(sog, args.max_cap)
        if spz_count(spz, args.max_cap) != count:
            raise ValueError('SOG and SPZ Gaussian counts differ')
        state.update(sog=str(sog), spz=str(spz), gaussians=count)
        state.pop('error', None)
        common.save_json(manifest, state)
        print(f"Finished: {sog} ({count:,} Gaussians)", flush=True)
        return 0
    except Exception as exc:
        if 'state' in locals():
            state['error'] = str(exc)
            common.save_json(manifest, state)
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
