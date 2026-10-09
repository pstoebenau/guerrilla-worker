import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import cv2
import numpy as np

import select_frames as sf


class SelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(123)
        cls.texture = rng.integers(0, 256, (360, 1000, 3), dtype=np.uint8)
        cls.texture = cv2.GaussianBlur(cls.texture, (3, 3), 0)
        for x in range(20, 980, 35):
            cv2.putText(cls.texture, str(x), (x, 180), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (255, 255, 255), 2)

    def args(self, *extra):
        return sf.parser().parse_args(["input.mp4", "out", "-n", "8", *extra])

    def candidates(self, offsets, blurry=()):
        orb = cv2.ORB_create(nfeatures=1000, fastThreshold=12)
        result = []
        for i, offset in enumerate(offsets):
            image = self.texture[:, offset:offset + 480].copy()
            if i in blurry:
                image = cv2.GaussianBlur(image, (31, 31), 7)
            result.append(sf.analyze_frame(i, i / 4, image, orb, 960))
        return result

    def test_stationary_clip_returns_one(self):
        selected, eligible, distinct = sf.select(self.candidates([0] * 25), self.args())
        self.assertEqual(eligible, 25)
        self.assertEqual(distinct, 1)
        self.assertEqual(len(selected), 1)

    def test_blur_rejection_cap_and_capture_coverage(self):
        frames = self.candidates(list(range(0, 480, 12)), blurry={8, 9, 10, 22})
        selected, _, distinct = sf.select(frames, self.args())
        self.assertGreater(distinct, 8)
        self.assertEqual(len(selected), 8)
        self.assertLessEqual(selected[0].index, 2)
        self.assertGreaterEqual(selected[-1].index, 37)
        self.assertFalse({8, 9, 10, 22} & {f.index for f in selected})
        self.assertEqual([f.index for f in selected], sorted(f.index for f in selected))

    def test_quality_wins_within_duplicate_group(self):
        frames = self.candidates([0] * 3)
        frames[0].sharpness *= 0.7
        frames[2].sharpness *= 0.8
        selected, _, _ = sf.select(frames, self.args())
        self.assertEqual([f.index for f in selected], [1])

    def test_one_image_budget(self):
        frames = self.candidates(list(range(0, 480, 20)))
        selected, _, _ = sf.select(frames, self.args("-n", "1"))
        self.assertEqual(len(selected), 1)

    def test_default_percentage_budget_and_explicit_override(self):
        args = sf.parser().parse_args(["input.mp4", "out"])
        self.assertEqual(args.sample_fps, 0)
        self.assertEqual(args.blur_ratio, 0.90)
        self.assertEqual(args.max_glare, 0.20)
        self.assertIsNone(args.max_images)
        self.assertEqual(sf.image_budget(1080, args), 324)
        args.max_images = None
        self.assertEqual(sf.image_budget(11, args), 4)
        self.assertEqual(sf.image_budget(1, args), 1)
        args.keep_percent = 50
        self.assertEqual(sf.image_budget(11, args), 6)
        args.max_images = 3
        self.assertEqual(sf.image_budget(1080, args), 3)

    def test_black_clip_has_no_usable_frames(self):
        orb = cv2.ORB_create()
        frames = [sf.analyze_frame(i, i / 4, np.zeros((240, 320, 3), np.uint8), orb, 960)
                  for i in range(8)]
        self.assertEqual(sf.select(frames, self.args())[0], [])

    def test_coverage_exceeds_percentage_and_retains_endpoints(self):
        frames = self.candidates(list(range(0, 480, 12)), blurry={8, 9, 10})
        args = self.args("--keep-percent", "2", "--preserve-coverage")
        args.max_images = None
        selected, _, _ = sf.select(frames, args)
        self.assertGreater(len(selected), sf.image_budget(len(frames), args))
        self.assertEqual((selected[0].index, selected[-1].index), (0, len(frames) - 1))
        self.assertLessEqual(max(b.seconds - a.seconds for a, b in zip(selected, selected[1:])), 0.5)
        # Unresolvable feature breaks must reach consecutive source candidates.
        for a, b in zip(selected, selected[1:]):
            if sf.compare(a, b)[0] < args.min_pair_overlap:
                self.assertEqual(b.index - a.index, 1)
        self.assertTrue(any(f.quality_rejection for f in selected))

    def test_trim_preserves_source_indices_and_timestamps(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "clip.mp4"
            self.write_video(source, [self.texture[:, :480].copy()] * 36)
            args = self.args("--start-seconds", "1", "--end-seconds", "2")
            frames, metadata = sf.scan_video(source, args)
            self.assertEqual([f.index for f in frames], list(range(12, 24)))
            self.assertGreaterEqual(frames[0].seconds, 1)
            self.assertLess(frames[-1].seconds, 2)
            self.assertFalse(metadata["warnings"])

    def test_unrecoverable_coverage_is_reported(self):
        frames = self.candidates([0] * 3)
        args = self.args("--preserve-coverage")
        args.max_images = None
        with patch.object(sf, "compare", return_value=(0.0, 1.0, 10.0)):
            selected, _, _ = sf.select(frames, args)
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                sf.write_reports(output, Path("input.mp4"), frames, selected, {}, {"warnings": []}, args)
                report = json.loads((output / "selection.json").read_text())
        self.assertFalse(report["coverage"]["checks_passed"])
        self.assertEqual(len(report["coverage"]["unresolved_pairs"]), 2)

    def test_coverage_rejects_hard_cap(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            sf.main(["input.mp4", "out", "--preserve-coverage", "--max-images", "3"])

    def test_glare_rejected_and_filter_can_be_disabled(self):
        orb = cv2.ORB_create(nfeatures=1000, fastThreshold=12)
        clean = self.texture[:, :480].copy()
        washed = np.clip(clean.astype(np.float32) * 0.6 + 95, 0, 255).astype(np.uint8)
        frames = [sf.analyze_frame(i, i / 4, image, orb, 960)
                  for i, image in enumerate([clean, clean, washed, clean])]
        sf.filter_quality(frames, self.args("--blur-ratio", "0.1"))
        self.assertGreater(frames[2].glare_score, 0.55)
        self.assertEqual(frames[2].reason, "glare_or_lens_flare")
        self.assertEqual(frames[0].reason, "eligible")
        sf.filter_quality(frames, self.args("--blur-ratio", "0.1", "--no-glare-filter"))
        self.assertEqual(frames[2].reason, "eligible")

    def test_coherent_purple_flare_and_clipped_highlights(self):
        clean = np.zeros((270, 480, 3), np.uint8)
        purple = clean.copy()
        purple[180:220, 100:380] = (180, 100, 150)
        white = clean.copy()
        white[50:180, 100:380] = 255
        self.assertEqual(sf.glare_metrics(clean)[1:], (0.0, 0.0))
        self.assertGreater(sf.glare_metrics(purple)[1], 0.04)
        self.assertGreater(sf.glare_metrics(white)[2], 0.12)

    def test_stable_bright_scene_not_mistaken_for_changing_veil(self):
        frames = self.candidates([0] * 8)
        for frame in frames:
            frame.shadow_floor = 100.0
            frame.purple_fraction = frame.highlight_fraction = 0.0
        self.assertEqual(len(sf.filter_quality(frames, self.args())), 8)

    def write_video(self, path, images):
        height, width = images[0].shape[:2]
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 12, (width, height))
        self.assertTrue(writer.isOpened(), "MP4 test encoder unavailable")
        try:
            for image in images:
                writer.write(image)
        finally:
            writer.release()

    def test_quality_95_jpeg_keeps_dimensions_and_reduces_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frame = self.candidates([0])[0]
            pixels = self.texture[:, :480].copy()
            _, png = sf.encode_png(root, frame, pixels, 1)
            _, jpg = sf.encode_png(root, frame, pixels, 1, 'jpg', 95)
            decoded = cv2.imdecode(np.frombuffer((root / jpg).read_bytes(), np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(decoded.shape, pixels.shape)
            self.assertGreater(cv2.PSNR(pixels, decoded), 30)
            self.assertLess((root / jpg).stat().st_size, (root / png).stat().st_size)

    def test_mp4_end_to_end_pngs_match_decoded_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "phone clip.mp4"
            output = root / "selected café"
            images = [self.texture[:, x:x + 480].copy() for x in range(0, 480, 8)]
            self.write_video(source, images)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = sf.main([str(source), str(output), "-n", "7", "--sample-fps", "0"])
            self.assertEqual(code, 0)
            report = json.loads((output / "selection.json").read_text())
            self.assertEqual(report["selected_count"], 7)
            self.assertEqual(len(list(output.glob("*.png"))), 7)
            self.assertEqual(report["decoded_frames"], 60)
            selected = {f["frame_index"]: f["filename"] for f in report["selected"]}
            cap = sf.open_video(source)
            try:
                for index in range(len(images)):
                    ok, frame = cap.read()
                    self.assertTrue(ok)
                    if index in selected:
                        png = cv2.imdecode(np.frombuffer((output / selected[index]).read_bytes(), np.uint8), cv2.IMREAD_COLOR)
                        self.assertEqual(png.shape, (360, 480, 3))
                        np.testing.assert_array_equal(png, frame)
            finally:
                cap.release()
            self.assertTrue((output / "candidates.csv").is_file())

    def test_sampling_and_zero_output_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "dark.mp4", root / "out"
            self.write_video(source, [np.zeros((240, 320, 3), np.uint8)] * 24)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = sf.main([str(source), str(output)])
            self.assertEqual(code, 2)
            report = json.loads((output / "selection.json").read_text())
            self.assertEqual(report["sampled_frames"], 24)
            self.assertEqual(report["resolved_max_images"], 8)
            self.assertEqual(report["selected_count"], 0)
            self.assertFalse(list(output.glob("*.png")))

    def test_parallel_export_owns_decoder_buffers_and_preserves_pixels(self):
        class ReusingDecoder:
            def __init__(self):
                self.index = -1
                self.image = np.zeros((32, 48, 3), np.uint8)
                self.released = False

            def grab(self):
                self.index += 1
                self.image.fill(self.index)
                return self.index < 4

            def retrieve(self):
                return True, self.image

            def release(self):
                self.released = True

        decoder = ReusingDecoder()
        barrier = threading.Barrier(2)
        encode = sf.encode_png

        def concurrent_encode(output, frame, image, compression, *options):
            # Requires two actual concurrent encoders; holds the first until
            # the decoder has reused its buffer for the second frame.
            barrier.wait(timeout=5)
            return encode(output, frame, image, compression, *options)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch.object(sf, "open_video", return_value=decoder), \
                    patch.object(sf, "encode_png", side_effect=concurrent_encode):
                exported = sf.export_pngs(Path("unused.mp4"), output,
                                          self.candidates([0] * 4), workers=2)
            self.assertTrue(decoder.released)
            self.assertEqual(list(exported), [0, 1, 2, 3])
            for index, filename in exported.items():
                pixels = cv2.imdecode(np.frombuffer((output / filename).read_bytes(), np.uint8),
                                     cv2.IMREAD_COLOR)
                np.testing.assert_array_equal(pixels, np.full((32, 48, 3), index, np.uint8))

    def test_worker_write_failure_is_reported_and_decoder_released(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clip.mp4"
            self.write_video(source, [self.texture[:, :480].copy()] * 4)
            decoder = sf.open_video(source)
            with patch.object(sf, "open_video", return_value=decoder), \
                    patch.object(sf, "encode_png", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    sf.export_pngs(source, root, self.candidates([0] * 4), workers=2)
            self.assertFalse(decoder.isOpened())

    def test_invalid_arguments_and_existing_output_are_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for option, value in [("-n", "0"), ("--sample-fps", "nan"), ("--blur-ratio", "2"), ("--max-glare", "nan"),
                                  ("--png-workers", "0"), ("--png-compression", "10"),
                                  ("--keep-percent", "0"), ("--keep-percent", "101"),
                                  ("--keep-percent", "nan")]:
                with self.assertRaises(SystemExit) as error:
                    sf.main(["video.mp4", "out", "-n", "3", option, value])
                self.assertEqual(error.exception.code, 2)
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "video.mp4"
                source.touch()
                output = root / "out"
                output.mkdir()
                sentinel = output / "keep.txt"
                sentinel.write_text("keep me")
                with self.assertRaises(SystemExit):
                    sf.main([str(source), str(output), "-n", "3"])
                self.assertEqual(sentinel.read_text(), "keep me")


if __name__ == "__main__":
    unittest.main()
