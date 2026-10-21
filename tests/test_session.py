"""--provider session: the request/response handoff with the Claude Code session."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402

spec = importlib.util.spec_from_file_location("broll_s", SCRIPTS / "04b_broll.py")
broll = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broll)

SCHEMA = {"type": "object", "properties": {"clips": {"type": "array"}}, "required": ["clips"]}


class Handoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(common, "OUTPUT_DIR", Path(self.tmp.name))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_request_is_written_and_pipeline_pauses(self):
        with self.assertRaises(SystemExit) as stop:
            common.await_session("scoring", "Score this transcript", SCHEMA)
        self.assertEqual(stop.exception.code, common.AWAITING_SESSION)
        req, _ = common.session_paths("scoring")
        text = req.read_text()
        rid = common.request_id("Score this transcript")
        self.assertIn(f'"request_id": "{rid}"', text)
        self.assertIn("Score this transcript", text)
        self.assertIn('"request_id"', text.split("## Task")[0], "schema asks for the echo field")

    def test_only_an_answer_to_this_exact_request_is_accepted(self):
        _, resp = common.session_paths("scoring")
        rid = common.request_id("prompt A")
        resp.write_text(json.dumps({"request_id": common.request_id("prompt B"), "clips": []}))
        self.assertIsNone(common.session_response("scoring", rid), "stale answer to another request")
        resp.write_text("{not json")
        self.assertIsNone(common.session_response("scoring", rid))
        resp.write_text(json.dumps({"request_id": rid, "clips": [1]}))
        self.assertEqual(common.session_response("scoring", rid)["clips"], [1])


class BrollSessionPicks(unittest.TestCase):
    words = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4} for i, w in enumerate("we flew to Mount Fuji and ate ramen in Tokyo".split())]

    def test_clean_picks_filters_and_snaps(self):
        moments = [
            {"at": 1.6, "phrase": "Mount Fuji", "query": "mount fuji"},  # snaps to "Mount" at 1.5
            {"at": 99.0, "phrase": "out of window", "query": "x"},
            {"at": 3.0, "phrase": "ramen", "query": "  "},
            "garbage",
        ]
        picks = broll.clean_picks(moments, self.words, 0.0, 10.0, 3)
        self.assertEqual(picks, [{"at": 1.5, "phrase": "Mount Fuji", "query": "mount fuji"}])


if __name__ == "__main__":
    unittest.main()
