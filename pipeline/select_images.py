"""Apply the repository's quality/duplicate filters to an ordered photo folder."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import re

import cv2
import numpy as np

import select_frames as sf

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


def image_paths(source):
    def natural_key(path):
        return [int(part) if part.isdigit() else part.casefold()
                for part in re.split(r"(\d+)", path.name)]

    paths = sorted((p for p in source.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES),
                   key=lambda p: (natural_key(p), p.name))
    if not paths:
        raise ValueError("Photo folder contains no JPG, JPEG, or PNG files")
    if any(p.is_symlink() or not p.is_file() for p in paths):
        raise ValueError("Photos must be regular files, not symlinks or directories")
    return paths


def read_image(path):
    image = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Cannot decode photo: {path.name}")
    return image


def select_images(source, output, max_images=None):
    paths = image_paths(source)
    args = sf.parser().parse_args([str(source), str(output)])
    # Photo sets may already be sparse: apply quality and redundancy filtering,
    # but don't discard 70% solely to match the dense-video candidate budget.
    args.keep_percent = 100.0
    args.max_images = max_images
    cv2.setRNGSeed(0)
    orb = cv2.ORB_create(nfeatures=1000, fastThreshold=12)
    frames = []
    shape = None
    for index, path in enumerate(paths):
        pixels = read_image(path)
        if shape is not None and pixels.shape != shape:
            raise ValueError("Single-camera photos must have the same dimensions and orientation")
        shape = pixels.shape
        # With index as the coordinate, the existing +/-2 relative sharpness
        # window compares five neighboring photos. This is not elapsed time.
        frames.append(sf.analyze_frame(index, float(index), pixels, orb, args.analysis_width))
        if index % 25 == 0:
            print(f"Scored {index + 1}/{len(paths)} photos", flush=True)
    selected, eligible, distinct = sf.select(frames, args)
    output.mkdir(parents=True, exist_ok=False)
    exports = {}
    for frame in selected:
        name = f"image_{frame.index:08d}.png"
        pixels = read_image(paths[frame.index])
        ok, encoded = cv2.imencode(".png", pixels, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if not ok:
            raise OSError(f"Cannot encode {name}")
        (output / name).write_bytes(encoded.tobytes())
        exports[frame.index] = name
    records = [{"sequence_index": f.index, "source_filename": paths[f.index].name,
                "filename": exports.get(f.index, ""), "reason": f.reason,
                "sharpness": f.sharpness, "quality_score": f.quality,
                "glare_score": f.glare_score, "features": len(f.points)} for f in frames]
    warnings = []
    if len(selected) < 3:
        warnings.append("Fewer than three photos survived filtering; reconstruction cannot start.")
    weak_pairs = sum(sf.compare(a, b)[0] < 0.2 for a, b in zip(selected, selected[1:]))
    if weak_pairs:
        warnings.append(f"{weak_pairs} adjacent selected pairs have few shared features.")
    report = {"source": str(source), "input_type": "ordered_photos", "input_count": len(paths),
              "selected_count": len(selected), "eligible_frames": eligible, "distinct_views": distinct,
              "resolved_max_images": sf.image_budget(len(paths), args),
              "selected": [r for r in records if r["reason"] == "selected"], "warnings": warnings,
              "settings": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
              "notes": ["Natural filename order; flat JPG/JPEG/PNG folder; one camera/lens.",
                        "Relative sharpness compares neighboring photo indices, not timestamps.",
                        "Full-resolution decoded 8-bit RGB pixels exported as PNG; no HDR color management.",
                        "Quality and feature-match scores are heuristics, not geometric overlap."]}
    (output / "selection.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    with (output / "candidates.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    print(f"Selected {len(selected)}/{len(paths)} photos", flush=True)
    return report
