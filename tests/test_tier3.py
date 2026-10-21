"""Tier 3 unit tests: the gated upload flow, against a fake HTTP layer."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("upload", SCRIPTS / "upload.py")
upload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upload)


class Workspace:
    """A temporary output/ with two rendered clips and their manifest."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        out = Path(self.tmp.name) / "output"
        self.clips = out / "clips"
        self.clips.mkdir(parents=True)
        manifest = {"clips": []}
        for rank in (1, 2):
            name = f"viral_{rank}_hook.mp4"
            (self.clips / name).write_bytes(b"fake mp4 " * rank)
            manifest["clips"].append(
                {
                    "rank": rank,
                    "file": name,
                    "hook_text": f"Hook {rank}",
                    "youtube_shorts": {"title": f"Title {rank}"},
                    "tiktok": {"description": "So good #fyp #learn"},
                    "instagram_reels": {"caption": f"Caption {rank} #reels"},
                    "broll": [{"credit": "Video by Ana on Pexels"}] if rank == 1 else [],
                }
            )
        (self.clips / "metadata.json").write_text(json.dumps(manifest))
        self.out = out
        self.patches = [
            mock.patch.object(upload, "CLIPS_DIR", self.clips),
            mock.patch.object(upload, "PLAN_JSON", out / "upload_plan.json"),
            mock.patch.object(upload, "UPLOADS_JSON", out / "uploads.json"),
        ]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def plan(self) -> dict:
        return json.loads((self.out / "upload_plan.json").read_text())


def args(**kw):
    defaults = {"platforms": "youtube", "privacy": "private", "only": None, "confirm": "", "force": False}
    return type("Args", (), {**defaults, **kw})()


class Plan(unittest.TestCase):
    def test_plan_contents(self):
        with Workspace() as ws, mock.patch("builtins.print"):
            upload.cmd_plan(args(platforms="youtube,instagram"))
            plan = ws.plan()
        first = plan["items"][0]
        self.assertEqual(first["youtube"]["title"], "Title 1")
        self.assertEqual(first["youtube"]["privacy"], "private")
        self.assertIn("#Shorts", first["youtube"]["description"])
        self.assertIn("Video by Ana on Pexels", first["youtube"]["description"], "B-roll credit is included")
        self.assertEqual(first["youtube"]["tags"], ["Shorts", "fyp", "learn"])
        self.assertEqual(first["instagram"]["visibility"], "public")
        self.assertEqual(len(plan["confirmation_code"]), 8)

    def test_code_changes_with_plan_or_files(self):
        with Workspace() as ws, mock.patch("builtins.print"):
            upload.cmd_plan(args())
            plan = ws.plan()
            code = upload.confirmation_code(plan)
            plan["items"][0]["youtube"]["privacy"] = "public"
            self.assertNotEqual(upload.confirmation_code(plan), code)
            plan["items"][0]["youtube"]["privacy"] = "private"
            os.utime(ws.clips / "viral_1_hook.mp4", (1, 1))  # a re-render changes the file
            self.assertNotEqual(upload.confirmation_code(plan), code)


class Run(unittest.TestCase):
    def test_refuses_without_the_right_code(self):
        with Workspace(), mock.patch("builtins.print"):
            upload.cmd_plan(args())
            with mock.patch.object(upload, "upload_youtube") as up, self.assertRaises(SystemExit):
                upload.cmd_run(args(confirm="00000000"))
            up.assert_not_called()

    def test_refuses_when_plan_was_edited_after_review(self):
        with Workspace() as ws, mock.patch("builtins.print"):
            upload.cmd_plan(args())
            plan = ws.plan()
            code = plan["confirmation_code"]
            plan["items"][0]["youtube"]["privacy"] = "public"
            (ws.out / "upload_plan.json").write_text(json.dumps(plan))
            with mock.patch.object(upload, "upload_youtube") as up, self.assertRaises(SystemExit):
                upload.cmd_run(args(confirm=code))
            up.assert_not_called()

    def test_uploads_once_and_records(self):
        with Workspace() as ws, mock.patch("builtins.print"):
            upload.cmd_plan(args())
            code = ws.plan()["confirmation_code"]
            fake = mock.Mock(side_effect=lambda it, tok: {"id": f"v{it['rank']}", "url": f"https://youtube.com/shorts/v{it['rank']}"})
            with mock.patch.object(upload, "youtube_access_token", return_value="tok"), mock.patch.object(upload, "upload_youtube", fake):
                upload.cmd_run(args(confirm=code))
                upload.cmd_run(args(confirm=code))  # second run: already uploaded, nothing re-posted
            self.assertEqual(fake.call_count, 2)
            log = json.loads((ws.out / "uploads.json").read_text())
            self.assertEqual(log["viral_1_hook.mp4"]["youtube"]["url"], "https://youtube.com/shorts/v1")


class Platforms(unittest.TestCase):
    def test_youtube_resumable_upload(self):
        calls = []

        def fake(method, url, headers=None, data=None):
            calls.append((method, url, headers, data))
            if method == "POST":
                return 200, {"location": "https://upload.example/session"}, b""
            return 200, {}, b'{"id": "abc123"}'

        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            f.write(b"x" * 10)
            f.flush()
            item = {"path": f.name, "youtube": {"title": "T", "description": "D", "tags": ["Shorts"], "privacy": "unlisted"}}
            with mock.patch.object(upload, "request", fake):
                out = upload.upload_youtube(item, "tok")
        self.assertEqual(out["url"], "https://youtube.com/shorts/abc123")
        init = calls[0]
        self.assertIn("uploadType=resumable", init[1])
        self.assertEqual(init[2]["X-Upload-Content-Length"], "10")
        self.assertEqual(json.loads(init[3])["status"]["privacyStatus"], "unlisted")
        self.assertEqual(calls[1][:2], ("PUT", "https://upload.example/session"))

    def test_instagram_container_upload_poll_publish(self):
        seen = []
        statuses = iter(["IN_PROGRESS", "FINISHED"])

        def fake(method, url, headers=None, data=None):
            seen.append((method, url, headers))
            if "rupload.facebook.com" in url:
                body = {"success": True}
            elif "/media_publish" in url:
                body = {"id": "m1"}
            elif method == "POST":
                body = {"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v23.0/c1"}
            elif "fields=status_code" in url:
                body = {"status_code": next(statuses)}
            else:
                body = {"permalink": "https://instagram.com/reel/m1"}
            return 200, {}, json.dumps(body).encode()

        env = {"IG_USER_ID": "u1", "IG_ACCESS_TOKEN": "t"}
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f, mock.patch.dict(os.environ, env), mock.patch.object(
            upload, "request", fake
        ), mock.patch.object(upload.time, "sleep"):
            f.write(b"y" * 7)
            f.flush()
            out = upload.upload_instagram({"path": f.name, "instagram": {"caption": "C"}})
        self.assertEqual(out, {"id": "m1", "url": "https://instagram.com/reel/m1"})
        create = seen[0]
        self.assertIn("media_type=REELS", create[1])
        self.assertIn("upload_type=resumable", create[1])
        upload_call = next(c for c in seen if "rupload" in c[1])
        self.assertEqual(upload_call[2]["file_size"], "7")
        self.assertEqual(upload_call[2]["Authorization"], "OAuth t")
        self.assertEqual(sum("fields=status_code" in c[1] for c in seen), 2, "polls until FINISHED")
        self.assertTrue(any("/media_publish" in c[1] for c in seen))

    def test_missing_credentials_are_reported(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            upload.youtube_access_token()


if __name__ == "__main__":
    unittest.main()
