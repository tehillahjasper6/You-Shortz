"""Tier 2 unit tests: face-track camera, B-roll picking/fetching, ducking maths."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), SCRIPTS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


face = load("04a_face_track.py")
broll = load("04b_broll.py")
master = load("04c_master_audio.py")


def f(cx, w=0.1):
    return {"cx": cx, "cy": 0.5, "w": w, "h": w * 1.7, "score": 0.9}


class SubjectChoice(unittest.TestCase):
    def test_stays_on_subject_until_a_bigger_face_persists(self):
        samples = [(i / 6, [f(0.3, 0.1)]) for i in range(6)]
        # A bigger face appears for only half a second: no switch.
        samples += [(1 + i / 6, [f(0.3, 0.1), f(0.75, 0.2)]) for i in range(3)]
        samples += [(1.5 + i / 6, [f(0.3, 0.1)]) for i in range(3)]
        # Then it stays for two seconds: switch after ~1 s.
        samples += [(2 + i / 6, [f(0.3, 0.1), f(0.75, 0.2)]) for i in range(12)]
        chosen = face.choose_subject(samples)
        xs = [c["cx"] for c in chosen]
        self.assertTrue(all(x == 0.3 for x in xs[:18]))
        self.assertEqual(xs[-1], 0.75)
        switch_t = samples[xs.index(0.75)][0]
        self.assertGreaterEqual(switch_t - 2, face.SWITCH_HOLD_S - 1e-9)

    def test_no_faces_gives_none(self):
        self.assertEqual(face.choose_subject([(0, []), (0.2, [])]), [None, None])


class CameraPath(unittest.TestCase):
    crop = 0.3164

    def test_far_jump_is_a_cut(self):
        times = [i / 6 for i in range(24)]
        targets = [0.3] * 12 + [0.75] * 12
        path = face.camera_path(times, targets, self.crop)
        cuts = [i for i, (_, cut) in enumerate(path) if cut]
        self.assertEqual(len(cuts), 1)
        self.assertAlmostEqual(path[-1][0], 0.75)

    def test_small_drift_is_ignored_then_recentred_smoothly(self):
        times = [i / 6 for i in range(60)]
        targets = [0.5] * 6 + [0.52] * 12 + [0.6] * 42
        path = face.camera_path(times, targets, self.crop)
        xs = [x for x, _ in path]
        self.assertEqual(xs[17], 0.5, "drift inside the dead zone doesn't move the camera")
        self.assertAlmostEqual(xs[-1], 0.6, places=2, msg="re-centres fully, not just to the dead-zone edge")
        steps = [b - a for a, b in zip(xs, xs[1:])]
        self.assertLessEqual(max(steps), face.MAX_PAN_PER_S / 6 + 1e-9)
        self.assertFalse(any(cut for _, cut in path))

    def test_window_stays_inside_frame_and_gaps_hold(self):
        path = face.camera_path([0, 0.2, 0.4], [0.01, None, None], self.crop)
        self.assertTrue(all(abs(x - self.crop / 2) < 1e-9 for x, _ in path))


class BrollPicking(unittest.TestCase):
    words = [
        {"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4}
        for i, w in enumerate(
            "so I flew to Mount Fuji last spring and then I bought the new iPhone 16 there . It was amazing honestly and then we went to Tokyo Tower".split()
        )
    ]

    def test_heuristic_finds_spaced_proper_nouns(self):
        picks = broll.pick_heuristic(self.words, 0.0, 30.0, 3)
        phrases = [p["phrase"] for p in picks]
        self.assertIn("Mount Fuji", phrases)
        self.assertNotIn("It", phrases, "sentence-initial capitals are not names")
        self.assertTrue(all(b["at"] - a["at"] >= broll.MIN_GAP_S for a, b in zip(picks, picks[1:])))

    def test_pexels_prefers_smallest_portrait_hd_file_and_skips_used(self):
        api = {
            "videos": [
                {"id": 1, "duration": 10, "video_files": [{"file_type": "video/mp4", "width": 1080, "height": 1920, "link": "u1"}]},
                {
                    "id": 2,
                    "duration": 10,
                    "url": "https://pexels.com/v/2",
                    "user": {"name": "Ana"},
                    "video_files": [
                        {"file_type": "video/mp4", "width": 2160, "height": 3840, "link": "big"},
                        {"file_type": "video/mp4", "width": 1080, "height": 1920, "link": "hd"},
                        {"file_type": "video/mp4", "width": 1920, "height": 1080, "link": "landscape"},
                    ],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as d, mock.patch.object(broll, "BROLL_DIR", Path(d)), mock.patch.object(
            broll, "http_json", return_value=api
        ), mock.patch.object(broll, "download") as dl:
            media = broll.fetch_pexels("mount fuji", "key", exclude={"1"})
        self.assertEqual(media["id"], "2")
        self.assertEqual(dl.call_args[0][0], "hd")
        self.assertEqual(media["credit"], "Video by Ana on Pexels")
        self.assertTrue(media["file"].startswith("broll/pexels_"))


class Ducking(unittest.TestCase):
    def test_threshold_gives_requested_reduction(self):
        speech = -14.0
        thr_db = 20 * __import__("math").log10(master.duck_threshold(speech, 12))
        detected = speech + master.DETECTOR_OFFSET_DB
        reduction = (detected - thr_db) * (1 - 1 / master.RATIO)
        self.assertAlmostEqual(reduction, 12.0, places=6)

    def test_threshold_is_clamped_to_ffmpeg_range(self):
        self.assertEqual(master.duck_threshold(-70, 40), master.MIN_THRESHOLD)

    def test_music_dir_cycles_by_rank(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("b.mp3", "a.wav", "notes.txt"):
                Path(d, n).write_bytes(b"")
            picks = [master.pick_music(Path(d), r).name for r in (1, 2, 3)]
        self.assertEqual(picks, ["a.wav", "b.mp3", "a.wav"])


if __name__ == "__main__":
    unittest.main()
