#!/usr/bin/env python3
"""Select reconstruction-friendly video frames without estimating camera poses."""
from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import csv
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import sys
import time

# Studio's embedded Python omits the script directory from sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline_defaults import MAX_IMAGES, KEEP_PERCENT, SAMPLE_FPS, BLUR_RATIO, MAX_GLARE

try:
    import cv2
    import numpy as np
except ImportError:
    raise SystemExit("Install dependencies first: python -m pip install -r requirements.txt")


@dataclass
class Frame:
    index: int
    seconds: float
    sharpness: float
    clipped: float
    contrast: float
    points: np.ndarray = field(repr=False)
    descriptors: np.ndarray | None = field(repr=False)
    thumbnail: np.ndarray = field(repr=False)
    quality: float = 0.0
    reason: str = ""
    shadow_floor: float = 0.0
    purple_fraction: float = 0.0
    highlight_fraction: float = 0.0
    glare_score: float = 0.0
    quality_rejection: str = ""


def glare_metrics(image):
    """Measure glare cues, not a calibrated probability or a pixel correction."""
    height, width = image.shape[:2]
    scale = min(1.0, 480 / max(height, width))
    small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    # A white veil lifts even the darkest channel in local neighborhoods.
    dark = cv2.erode(small.min(axis=2), np.ones((15, 15), np.uint8))
    shadow_floor = float(np.median(dark))
    b, g, r = cv2.split(small.astype(np.float32))
    purple = ((b - g > 12) & (r - g > 5) & (b > 65)).astype(np.uint8)
    # Require spatially coherent patches; ignore isolated colored texture.
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    purple = cv2.morphologyEx(purple, cv2.MORPH_OPEN, kernel)
    highlights = (small.min(axis=2) >= 245).astype(np.uint8)
    highlights = cv2.morphologyEx(highlights, cv2.MORPH_OPEN, kernel)
    return shadow_floor, float(purple.mean()), float(highlights.mean())


def open_video(path: Path):
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        raise ValueError(f"Cannot decode video: {path}. Check the file and codec support.")
    # Explicitly enable rotation metadata, including portrait phone recordings.
    cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
    return cap


def analyze_frame(index, seconds, image, orb, analysis_width):
    height, width = image.shape[:2]
    scale = min(1.0, analysis_width / max(height, width))
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if scale < 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    # Mild denoising reduces the chance of mistaking sensor noise for sharp detail.
    smooth = cv2.GaussianBlur(gray, (3, 3), 0)
    sharpness = float(cv2.Laplacian(smooth, cv2.CV_32F).var())
    clipped = float(np.mean((gray <= 5) | (gray >= 250)))
    contrast = float(np.std(gray))
    keypoints, descriptors = orb.detectAndCompute(gray, None)
    points = np.array([k.pt for k in keypoints], dtype=np.float32).reshape(-1, 2)
    # Coordinates normalized by the image diagonal; this never warps any image.
    points /= math.hypot(*gray.shape)
    thumbnail = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA)
    frame = Frame(index, seconds, sharpness, clipped, contrast, points, descriptors, thumbnail)
    frame.shadow_floor, frame.purple_fraction, frame.highlight_fraction = glare_metrics(image)
    return frame


def scan_video(path, args):
    cap = open_video(path)
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    expected = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if not math.isfinite(fps) or fps <= 0:
        cap.release()
        raise ValueError("Video has no valid frame rate; transcode it before selecting frames.")
    orb = cv2.ORB_create(nfeatures=1000, fastThreshold=12)
    frames = []
    index = 0
    next_sample = 0.0
    last_time = -1.0
    last_progress = time.monotonic()
    timestamp_fallbacks = 0
    try:
        while cap.grab():
            # Prefer decoder timestamps for variable-frame-rate phone videos.
            seconds = float(cap.get(cv2.CAP_PROP_POS_MSEC)) / 1000
            if not math.isfinite(seconds) or seconds < 0 or (index > 0 and seconds <= last_time):
                seconds = max(index / fps, last_time + 1 / fps)
                timestamp_fallbacks += 1
            last_time = seconds
            if args.end_seconds is not None and seconds >= args.end_seconds:
                break
            if seconds >= args.start_seconds and (args.sample_fps == 0 or seconds + 1e-6 >= next_sample):
                ok, image = cap.retrieve()
                if not ok:
                    raise ValueError(f"Failed to decode frame {index}.")
                frames.append(analyze_frame(index, seconds, image, orb, args.analysis_width))
                if args.sample_fps:
                    next_sample = (math.floor(seconds * args.sample_fps) + 1) / args.sample_fps
            index += 1
            if time.monotonic() - last_progress > 5:
                print(f"Analyzed {len(frames)} candidates; scanned {index}/{expected or '?'} frames...", flush=True)
                last_progress = time.monotonic()
    finally:
        cap.release()
    if not frames:
        raise ValueError("The video contains no decodable frames.")
    warnings = []
    if args.end_seconds is None and expected > 0 and index < expected - max(2, expected * 0.01):
        warnings.append(f"Decoder stopped at frame {index}; metadata reports {expected}. Input may be truncated.")
    if timestamp_fallbacks:
        warnings.append(f"Used nominal frame-rate timestamps for {timestamp_fallbacks} frames because decoder timestamps were unavailable/nonmonotonic.")
    return frames, {"fps": fps, "decoded_frames": index, "sampled_frames": len(frames),
                    "duration_seconds": last_time + 1 / fps,
                    "selection_start_seconds": frames[0].seconds,
                    "selection_end_seconds": frames[-1].seconds, "warnings": warnings}


def filter_quality(frames, args):
    if not frames:
        return []
    sharp = np.array([f.sharpness for f in frames])
    times = np.array([f.seconds for f in frames])
    # Relative to this capture's clearer frames, rather than assuming every
    # scene must contain deep black pixels (e.g. a well-lit pale object).
    shadow_baseline = float(np.percentile([f.shadow_floor for f in frames], 10))
    for i, frame in enumerate(frames):
        # Local comparison avoids penalizing an entire low-texture part of a scene.
        left = np.searchsorted(times, frame.seconds - 2.0)
        right = np.searchsorted(times, frame.seconds + 2.0, side="right")
        reference = max(float(np.percentile(sharp[left:right], 80)), 1e-6)
        veil = np.clip((frame.shadow_floor - shadow_baseline - 12) / 65, 0, 1)
        purple = np.clip(frame.purple_fraction / 0.04, 0, 1)
        highlights = np.clip(frame.highlight_fraction / 0.12, 0, 1)
        frame.glare_score = float(max(veil, purple, highlights))
        if not args.no_glare_filter and frame.glare_score > args.max_glare:
            frame.reason = "glare_or_lens_flare"
        elif frame.sharpness < args.min_sharpness:
            frame.reason = "below_absolute_sharpness"
        elif frame.sharpness < args.blur_ratio * reference:
            frame.reason = "blurrier_than_nearby_frames"
        elif frame.clipped > args.max_clipped:
            frame.reason = "mostly_clipped_exposure"
        elif frame.contrast < 4:
            frame.reason = "too_little_contrast"
        elif len(frame.points) < args.min_features:
            frame.reason = "too_few_features"
        else:
            frame.reason = "eligible"
        frame.quality_rejection = "" if frame.reason == "eligible" else frame.reason
        relative = min(frame.sharpness / reference, 1.5) / 1.5
        detail = min(len(frame.points) / 500, 1.0)
        frame.quality = float(0.65 * relative + 0.25 * detail + 0.1 * (1 - frame.clipped))
        if not args.no_glare_filter:
            frame.quality *= 1 - 0.6 * frame.glare_score
    return [f for f in frames if f.reason == "eligible"]


def compare(a, b):
    """Return feature-match fraction and median displacement; no pose/alignment."""
    delta = float(np.mean(np.abs(a.thumbnail.astype(np.float32) - b.thumbnail)))
    if a.descriptors is None or b.descriptors is None:
        return 0.0, 1.0, delta
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = [m for m in matcher.match(a.descriptors, b.descriptors) if m.distance <= 48]
    if len(matches) < 12:
        return 0.0, 1.0, delta
    overlap = len(matches) / max(len(a.points), len(b.points))
    shifts = [np.linalg.norm(a.points[m.queryIdx] - b.points[m.trainIdx]) for m in matches]
    return float(overlap), float(np.median(shifts)), delta


def distinct_frames(frames, args):
    if not frames:
        return []
    representatives = []
    anchor = best = frames[0]
    for candidate in frames[1:]:
        overlap, motion, delta = compare(anchor, candidate)
        same_view = delta < 2.0 or (overlap >= args.match_overlap and motion < args.min_motion)
        if same_view:
            if candidate.quality > best.quality:
                best = candidate
        else:
            representatives.append(best)
            anchor = best = candidate
    representatives.append(best)
    for frame in frames:
        frame.reason = "redundant_nearby_view"
    for frame in representatives:
        frame.reason = "over_image_budget"
    return representatives


def limit_frames(frames, count):
    if len(frames) <= count:
        return frames
    if count == 1:
        return [max(frames, key=lambda f: f.quality)]
    # Parameterize the capture by visual change, not elapsed time: pauses should
    # not consume a disproportionate part of the image budget.
    position = [0.0]
    for a, b in zip(frames, frames[1:]):
        overlap, motion, _ = compare(a, b)
        distance = max(0.01, min(motion, 0.25), 0.10 * (1 - overlap))
        position.append(position[-1] + distance)
    position = np.array(position)
    selected = {0, len(frames) - 1}
    nearest = np.minimum(position - position[0], position[-1] - position)
    quality = np.array([0.75 + 0.25 * f.quality for f in frames])
    while len(selected) < count:
        scores = nearest * quality
        scores[list(selected)] = -1
        idx = int(np.argmax(scores))
        selected.add(idx)
        nearest = np.minimum(nearest, np.abs(position - position[idx]))
    return [frames[i] for i in sorted(selected)]


def image_budget(candidate_count, args):
    if args.max_images is not None:
        return args.max_images
    return max(1, math.ceil(candidate_count * args.keep_percent / 100))


def select(frames, args):
    eligible = filter_quality(frames, args)
    distinct = distinct_frames(eligible, args)
    selected = limit_frames(distinct, image_budget(len(frames), args))
    if getattr(args, "preserve_coverage", False):
        selected = preserve_coverage(frames, selected, args)
    for frame in selected:
        frame.reason = "selected"
    return selected, len(eligible), len(distinct)


def preserve_coverage(frames, selected, args):
    """Bridge temporal/feature gaps, retaining diagnostics for unavoidable breaks.

    Quality filters rank the initial picks. Coverage can reinstate rejected frames;
    it cannot recover features missing even between consecutive source frames.
    Explicit image caps are not compatible with this mode.
    """
    if not frames:
        return []
    positions = {f.index: i for i, f in enumerate(frames)}
    picks = {positions[f.index] for f in selected} | {0, len(frames) - 1}
    ordered = sorted(picks)
    pending = list(zip(ordered, ordered[1:]))
    while pending:
        left, right = pending.pop()
        a, b = frames[left], frames[right]
        if right == left + 1:
            continue
        if b.seconds - a.seconds <= args.max_gap_seconds and compare(a, b)[0] >= args.min_pair_overlap:
            continue
        # Choose a clear frame near the midpoint, guaranteeing progress even
        # through runs rejected by the original quality filters.
        span = right - left
        lo = left + max(1, span // 3)
        hi = min(right, left + (2 * span) // 3 + 1)
        middle = max(range(lo, hi), key=lambda i: frames[i].quality)
        picks.add(middle)
        pending.extend(((left, middle), (middle, right)))
    result = [frames[i] for i in sorted(picks)]
    # A poor source frame can match neither neighbor even when the neighbors
    # match each other. Remove such short runs instead of forcing a bad link.
    i = 0
    while i < len(result) - 1:
        a, b = result[i:i + 2]
        if compare(a, b)[0] >= args.min_pair_overlap:
            i += 1
            continue
        shortcuts = []
        for left in range(i, -1, -1):
            if b.seconds - result[left].seconds > args.max_gap_seconds:
                break
            for right in range(i + 1, len(result)):
                if result[right].seconds - result[left].seconds > args.max_gap_seconds:
                    break
                if right - left > 1 and compare(result[left], result[right])[0] >= args.min_pair_overlap:
                    shortcuts.append((right - left, left, right))
        if shortcuts:
            _, left, right = min(shortcuts)
            del result[left + 1:right]
            i = max(0, left - 1)
        else:
            i += 1
    return result


def encode_png(output, frame, image, compression, image_format='png', jpeg_quality=95):
    filename = f"frame_{frame.index:08d}_{frame.seconds:010.3f}s.{image_format}"
    params = [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality,
              cv2.IMWRITE_JPEG_SAMPLING_FACTOR, cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444] if image_format == 'jpg' else [cv2.IMWRITE_PNG_COMPRESSION, compression]
    ok, encoded = cv2.imencode('.' + image_format, image, params)
    if not ok:
        raise ValueError(f"PNG encoding failed for frame {frame.index}.")
    # Path.write_bytes handles Unicode Windows paths reliably.
    (output / filename).write_bytes(encoded.tobytes())
    return frame.index, filename


def export_pngs(path, output, frames, workers=4, compression=1, image_format='png', jpeg_quality=95):
    if workers < 1 or not 0 <= compression <= 9:
        raise ValueError("PNG workers must be positive and compression must be 0-9")
    if not frames:
        return {}
    cap = open_video(path)
    pending = {f.index: f for f in frames}
    exported = {}
    index = 0
    jobs = deque()

    def collect_oldest():
        frame_index, filename = jobs.popleft().result()
        exported[frame_index] = filename
        if len(exported) % 25 == 0:
            print(f"Exported {len(exported)}/{len(frames)} {'JPEGs' if image_format == 'jpg' else 'PNGs'}...", flush=True)

    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="png") as pool:
            try:
                # Keep decoding sequential for exact indices; overlap it with PNG work.
                while pending and cap.grab():
                    if index in pending:
                        ok, image = cap.retrieve()
                        if not ok:
                            raise ValueError(f"Failed to decode selected frame {index} during export.")
                        frame = pending.pop(index)
                        # Own the pixels even if a decoder reuses its output buffer.
                        jobs.append(pool.submit(encode_png, output, frame, image.copy(), compression, image_format, jpeg_quality))
                        # Bound outstanding full-resolution frames, including active workers.
                        if len(jobs) >= workers:
                            collect_oldest()
                    index += 1
                while jobs:
                    collect_oldest()
            except BaseException:
                for job in jobs:
                    job.cancel()
                raise
    finally:
        cap.release()
    if pending:
        raise ValueError(f"Video ended before {len(pending)} selected frames could be exported.")
    return exported


def write_reports(output, source, frames, selected, exported, metadata, args):
    pairs = []
    for a, b in zip(selected, selected[1:]):
        overlap, motion, _ = compare(a, b)
        pairs.append({"from_frame": a.index, "to_frame": b.index,
                      "from_seconds": a.seconds, "to_seconds": b.seconds,
                      "gap_seconds": b.seconds - a.seconds,
                      "feature_match_fraction": overlap, "median_displacement_diagonal": motion})
    weak_pairs = sum(p["feature_match_fraction"] < 0.2 for p in pairs)
    if weak_pairs:
        metadata["warnings"].append(f"{weak_pairs} adjacent selected pairs have few shared features. Inspect these intervals; a larger percentage or coverage bridging may help, but missing source detail cannot be recovered.")
    if args.preserve_coverage:
        unresolved = [p for p in pairs if p["gap_seconds"] > args.max_gap_seconds + 1e-6
                      or p["feature_match_fraction"] < args.min_pair_overlap]
        metadata["coverage"] = {
            "max_gap_seconds": args.max_gap_seconds,
            "min_pair_overlap": args.min_pair_overlap,
            "target_images": image_budget(len(frames), args),
            "exceeded_target_by": max(0, len(selected) - image_budget(len(frames), args)),
            "unresolved_pairs": unresolved,
            "checks_passed": bool(selected) and not unresolved,
        }
        if unresolved:
            metadata["warnings"].append(f"Coverage remains unresolved at {len(unresolved)} pairs even after adding source frames; inspect or trim these intervals. No gap-free reconstruction is guaranteed.")
    if not selected:
        metadata["warnings"].append("No frames passed the quality filters; inspect the video and candidates.csv before adjusting thresholds.")
    metadata["glare_rejected_frames"] = sum(f.reason == "glare_or_lens_flare" for f in frames)
    metadata["max_selected_gap_seconds"] = max((b.seconds - a.seconds for a, b in zip(selected, selected[1:])), default=0.0)
    if metadata["max_selected_gap_seconds"] > 2:
        metadata["warnings"].append(f"Largest gap between selected frames is {metadata['max_selected_gap_seconds']:.2f}s. Rejecting glare/blur can leave gaps in coverage; inspect the report before reconstruction.")
    records = []
    for frame in frames:
        records.append({"frame_index": frame.index, "timestamp_seconds": round(frame.seconds, 6),
                        "sharpness": round(frame.sharpness, 4), "clipped_fraction": round(frame.clipped, 4),
                        "features": len(frame.points), "quality_score": round(frame.quality, 4),
                        "glare_score": round(frame.glare_score, 4),
                        "shadow_floor": round(frame.shadow_floor, 4),
                        "purple_fraction": round(frame.purple_fraction, 6),
                        "highlight_fraction": round(frame.highlight_fraction, 6),
                        "reason": frame.reason, "filename": exported.get(frame.index, "")})
        records[-1]["quality_rejection"] = frame.quality_rejection
    with (output / "candidates.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    report = {"source": str(source), "settings": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
              **metadata, "selected_count": len(selected),
              "selected": [r for r in records if r["reason"] == "selected"], "adjacent_pairs": pairs,
              "notes": ["Feature match fractions are heuristics, not geometric overlap percentages.",
                        "No alignment, camera poses, or 3D reconstruction are computed.",
                        "Glare scores are heuristic: haze, pale objects, purple surfaces, or bright sky can trigger them. Subtle or persistent flare may be missed. Source pixels are not edited.",
                        f"Images: {getattr(args, 'image_format', 'png')}; JPEG quality: {getattr(args, 'jpeg_quality', 95)}. PNG preserves decoded pixels; JPEG uses lossy compression."]}
    (output / "selection.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("video", type=Path, help="Input video, such as a phone MP4")
    p.add_argument("output", type=Path, help="New or empty output directory")
    p.add_argument("-n", "--max-images", type=int, default=MAX_IMAGES,
                   help="Explicit maximum PNG count; overrides --keep-percent")
    p.add_argument("--keep-percent", type=float, default=KEEP_PERCENT,
                   help="Percentage of sampled candidates before filtering, rounded up; soft target with --preserve-coverage, otherwise a cap; ignored with -n")
    p.add_argument("--start-seconds", type=float, default=0.0, help="Inclusive selection start in original-video seconds")
    p.add_argument("--end-seconds", type=float, help="Exclusive selection end; the original video is preserved")
    p.add_argument("--preserve-coverage", action="store_true", help="Exceed the percentage target to bridge temporal and feature gaps; may reinstate lower-quality frames")
    p.add_argument("--max-gap-seconds", type=float, default=0.5, help="Maximum temporal gap in coverage mode")
    p.add_argument("--min-pair-overlap", type=float, default=0.2, help="Minimum heuristic shared-feature fraction in coverage mode; not geometric overlap")
    p.add_argument("--sample-fps", type=float, default=SAMPLE_FPS, help="Candidates per second; 0 analyzes every frame")
    p.add_argument("--analysis-width", type=int, default=960, help="Maximum analysis dimension; exports remain full resolution")
    p.add_argument("--min-sharpness", type=float, default=8.0, help="Minimum denoised Laplacian variance")
    p.add_argument("--blur-ratio", type=float, default=BLUR_RATIO, help="Minimum sharpness relative to nearby frames")
    p.add_argument("--max-clipped", type=float, default=0.85, help="Maximum fraction of near-black/white pixels")
    p.add_argument("--max-glare", type=float, default=MAX_GLARE, help="Maximum heuristic glare score; lower values reject more (try 0.35 for strong filtering)")
    p.add_argument("--no-glare-filter", action="store_true", help="Disable glare rejection and glare quality penalty")
    p.add_argument("--min-features", type=int, default=40, help="Minimum detected ORB features")
    p.add_argument("--min-motion", type=float, default=0.025, help="View change threshold as a fraction of image diagonal")
    p.add_argument("--match-overlap", type=float, default=0.45, help="Shared-feature fraction required to classify nearby views as redundant")
    p.add_argument("--png-workers", type=int, default=min(4, os.cpu_count() or 1),
                   help="Concurrent PNG encoders/writers; also bounds queued full-resolution frames")
    p.add_argument("--png-compression", type=int, choices=range(10), default=1,
                   help="Lossless PNG compression level: 0 fastest/largest, 9 slowest/smallest")
    p.add_argument('--image-format', choices=('png', 'jpg'), default='png')
    p.add_argument('--jpeg-quality', type=int, choices=range(1, 101), default=95)
    return p


def main(argv=None):
    p = parser()
    cli_args = list(sys.argv[1:] if argv is None else argv)
    args = p.parse_args(cli_args)
    if args.preserve_coverage and args.max_images is not None:
        p.error("--preserve-coverage uses a soft percentage target and cannot be combined with --max-images")
    if not math.isfinite(args.start_seconds) or args.start_seconds < 0:
        p.error("--start-seconds must be finite and nonnegative")
    if args.end_seconds is not None and (not math.isfinite(args.end_seconds) or args.end_seconds <= args.start_seconds):
        p.error("--end-seconds must be finite and greater than --start-seconds")
    if not math.isfinite(args.max_gap_seconds) or args.max_gap_seconds <= 0:
        p.error("--max-gap-seconds must be finite and positive")
    if not math.isfinite(args.min_pair_overlap) or not 0 < args.min_pair_overlap <= 1:
        p.error("--min-pair-overlap must be in (0, 1]")
    if any(x.split("=")[0] == "--keep-percent" for x in cli_args) and not any(x.split("=")[0] in ("-n", "--max-images") for x in cli_args):
        args.max_images = None
    if args.max_images is not None and args.max_images < 1:
        p.error("--max-images must be at least 1")
    if not math.isfinite(args.keep_percent) or not 0 < args.keep_percent <= 100:
        p.error("--keep-percent must be in (0, 100]")
    if args.png_workers < 1:
        p.error("--png-workers must be at least 1")
    if not math.isfinite(args.sample_fps) or args.sample_fps < 0:
        p.error("--sample-fps must be finite and nonnegative")
    if args.analysis_width < 128 or args.min_features < 0:
        p.error("--analysis-width must be >= 128 and --min-features must be >= 0")
    if not math.isfinite(args.min_sharpness) or args.min_sharpness < 0:
        p.error("--min-sharpness must be finite and nonnegative")
    for name in ("blur_ratio", "max_clipped", "min_motion", "match_overlap", "max_glare"):
        value = getattr(args, name)
        if not math.isfinite(value) or not 0 < value <= 1:
            p.error(f"--{name.replace('_', '-')} must be in (0, 1]")
    source = args.video.resolve()
    output = args.output.resolve()
    if not source.is_file():
        p.error(f"Input video does not exist: {source}")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        p.error("Output must be a new or empty directory; existing files will not be overwritten")
    try:
        cv2.setRNGSeed(0)
        print("Scanning video and scoring candidate frames...", flush=True)
        frames, metadata = scan_video(source, args)
        budget = image_budget(len(frames), args)
        print(f"Selecting from {len(frames)} candidates ({'target' if args.preserve_coverage else 'maximum'} {budget} images)...", flush=True)
        selected, eligible_count, distinct_count = select(frames, args)
        metadata.update(eligible_frames=eligible_count, distinct_views=distinct_count,
                        resolved_max_images=budget)
        label = 'JPEGs' if args.image_format == 'jpg' else 'PNGs'
        print(f"{eligible_count} candidates passed quality checks; {distinct_count} distinct nearby views. Exporting {len(selected)} {label}...", flush=True)
        output.mkdir(parents=True, exist_ok=True)
        exported = export_pngs(source, output, selected, args.png_workers, args.png_compression, args.image_format, args.jpeg_quality)
        write_reports(output, source, frames, selected, exported, metadata, args)
        print(f"Saved {len(selected)} / {budget} {label} to {output}")
        for warning in metadata["warnings"]:
            print(f"Warning: {warning}", file=sys.stderr)
        return 0 if selected else 2
    except (ValueError, OSError, cv2.error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
