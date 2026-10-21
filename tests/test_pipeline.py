"""Unit tests for the Python phases: python3 -m unittest discover -s tests"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), SCRIPTS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


transcribe = load("02_transcribe.py")
detect = load("03_detect_viral.py")


def words_at(t0: float, n: int, step: float = 0.35) -> list[dict]:
    return [{"word": f"w{t0:.0f}_{i}", "start": round(t0 + i * step, 3), "end": round(t0 + (i + 1) * step, 3)} for i in range(n)]


class Json3Parsing(unittest.TestCase):
    def parse(self, events):
        with tempfile.NamedTemporaryFile("w", suffix=".json3", delete=False) as f:
            json.dump({"events": events}, f)
        return transcribe.words_from_json3(Path(f.name))

    def test_auto_captions_use_word_offsets(self):
        words = self.parse(
            [
                {"tStartMs": 1000, "dDurationMs": 3000, "segs": [{"utf8": "hello"}, {"utf8": " big", "tOffsetMs": 400}, {"utf8": " world", "tOffsetMs": 900}]},
                {"tStartMs": 2500, "dDurationMs": 500, "aAppend": 1, "segs": [{"utf8": "\n"}]},
                {"tStartMs": 3000, "dDurationMs": 2000, "segs": [{"utf8": "[Music]"}]},
            ]
        )
        self.assertEqual([w["word"] for w in words], ["hello", "big", "world"])
        self.assertEqual(words[0]["start"], 1.0)
        self.assertAlmostEqual(words[1]["start"], 1.4)
        self.assertAlmostEqual(words[2]["end"], 2.5, msg="last word ends when the next cue starts")

    def test_manual_captions_spread_words_across_cue(self):
        words = self.parse([{"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "one two three four"}]}])
        self.assertEqual(len(words), 4)
        self.assertEqual(words[0]["start"], 0)
        self.assertAlmostEqual(words[-1]["end"], 2.0)
        self.assertTrue(all(a["end"] <= b["start"] + 1e-9 for a, b in zip(words, words[1:])))

    def test_segments_split_on_pauses_and_punctuation(self):
        ws = [
            {"word": "Hi.", "start": 0.0, "end": 0.3},
            {"word": "so", "start": 0.35, "end": 0.6},
            {"word": "then", "start": 0.65, "end": 0.9},
            {"word": "later", "start": 2.0, "end": 2.3},
        ]
        segs = transcribe.segments_from_words(ws)
        self.assertEqual([s["text"] for s in segs], ["Hi.", "so then", "later"])


class Validation(unittest.TestCase):
    def clip(self, **kw):
        c = {
            "start": 10.0, "end": 40.0, "hook_text": "A short hook", "hook_variants": [], "reasoning": "r",
            "tiktok_description": "t", "instagram_caption": "i", "youtube_title": "y",
        }
        c.update(kw)
        return c

    def test_valid_payload(self):
        self.assertEqual(detect.validate({"clips": [self.clip()]}, 100.0, 1), [])

    def test_contract_violations(self):
        cases = {
            "negative": self.clip(start=-1.0),
            "past end": self.clip(end=120.0),
            "reversed": self.clip(start=50.0, end=40.0),
            "too short": self.clip(end=20.0),
            "too long": self.clip(end=80.0),
            "long hook": self.clip(hook_text="one two three four five six seven eight nine ten eleven"),
            "long title": self.clip(youtube_title="x" * 101),
        }
        for name, c in cases.items():
            with self.subTest(name):
                self.assertTrue(detect.validate({"clips": [c]}, 100.0, 1))

    def test_overlap_and_count(self):
        errors = detect.validate({"clips": [self.clip(), self.clip(start=30.0, end=55.0)]}, 100.0, 3)
        self.assertTrue(any("overlap" in e for e in errors))
        self.assertTrue(any("expected 3" in e for e in errors))


class Snapping(unittest.TestCase):
    def setUp(self):
        # Sentences of 10 words every 4.1 s with a 0.6 s pause between them.
        self.segments = []
        for k in range(20):
            ws = words_at(4.1 * k, 10)
            self.segments.append({"text": " ".join(w["word"] for w in ws), "start": ws[0]["start"], "end": ws[-1]["end"], "words": ws})
        self.words = [w for s in self.segments for w in s["words"]]
        self.silences = [(4.1 * k + 3.5, 4.1 * (k + 1)) for k in range(20)]

    def test_mid_word_boundaries_snap_to_phrase_edges_with_padding(self):
        # Model returned a start in the middle of a sentence and an end mid-word.
        out = detect.snap_clip({"start": 16.9, "end": 41.2}, self.words, self.segments, 90.0, self.silences)
        first = min((w for w in self.words if w["start"] >= out["start"]), key=lambda w: w["start"])
        last = max((w for w in self.words if w["end"] <= out["end"]), key=lambda w: w["end"])
        self.assertAlmostEqual(first["start"] - out["start"], 0.3, places=3)
        self.assertAlmostEqual(out["end"] - last["end"], 0.3, places=3)
        self.assertAlmostEqual(first["start"], 16.4, places=3, msg="starts on the sentence, not mid-phrase")
        self.assertFalse(any(w["start"] < out["start"] < w["end"] or w["start"] < out["end"] < w["end"] for w in self.words))
        self.assertEqual((out["llm_start"], out["llm_end"]), (16.9, 41.2))

    def test_snapping_keeps_duration_in_bounds(self):
        out = detect.snap_clip({"start": 0.0, "end": 59.9}, self.words, self.segments, 90.0, self.silences)
        self.assertLessEqual(out["end"] - out["start"], detect.MAX_CLIP_S)
        self.assertGreaterEqual(out["end"] - out["start"], detect.MIN_CLIP_S)

    def test_heuristic_provider_returns_valid_non_overlapping_clips(self):
        transcript = {"segments": self.segments, "duration": 82.0}
        payload = detect.heuristic_candidates(transcript, 3, 82.0)
        self.assertEqual(detect.validate(payload, 82.0, 3), [])


if __name__ == "__main__":
    unittest.main()
