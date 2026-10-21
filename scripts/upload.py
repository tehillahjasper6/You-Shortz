#!/usr/bin/env python3
"""Phase 6 — Upload rendered clips with their platform metadata (opt-in, gated).

Two steps, so nothing is ever posted by accident:

  1. plan   Writes output/upload_plan.json (which file goes where, with which
            title/caption/privacy) and prints it with a confirmation code.
  2. run    Uploads only when given that code. The code is a hash of the plan
            and of the clip files, so a changed plan or a re-render needs a
            fresh review.

  python3 scripts/upload.py plan --platforms youtube,instagram --privacy private
  python3 scripts/upload.py run --confirm 3f9a1c2e

Platforms and credentials (environment variables):
  youtube    YouTube Data API v3 resumable upload, as a Short.
             YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN
             (OAuth refresh token with the youtube.upload scope).
             --privacy private (default) | unlisted | public.
  instagram  Instagram Graph API Reels, resumable upload, then publish.
             IG_USER_ID, IG_ACCESS_TOKEN (instagram_content_publish).
             Instagram has no private posts: publishing makes the Reel public.

Every upload is recorded in output/uploads.json; a clip already uploaded to a
platform is skipped unless --force.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from common import CLIPS_DIR, OUTPUT_DIR, log, read_json, rel, write_json

PLAN_JSON = OUTPUT_DIR / "upload_plan.json"
UPLOADS_JSON = OUTPUT_DIR / "uploads.json"
PLATFORMS = ("youtube", "instagram")
GRAPH = "https://graph.facebook.com"
IG_POLL_S = 5
IG_POLL_MAX_S = 600


# --------------------------------------------------------------------------- HTTP


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url.split('?')[0]}: {body[:500]}")
        self.status = status


def request(method: str, url: str, headers: dict | None = None, data: bytes | None = None) -> tuple[int, dict, bytes]:
    """One HTTP call; returns (status, lower-cased headers, body). Raises HttpError on 4xx/5xx."""
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "viral-clipper/0.3", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode("utf-8", "replace"), url) from None


def request_json(method: str, url: str, headers: dict | None = None, data: bytes | None = None) -> dict:
    _, _, body = request(method, url, headers, data)
    return json.loads(body or b"{}")


def require_env(*names: str) -> dict:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise RuntimeError(f"missing environment variable(s): {', '.join(missing)}")
    return {n: os.environ[n] for n in names}


# --------------------------------------------------------------------------- YouTube


def youtube_access_token() -> str:
    env = require_env("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")
    form = urllib.parse.urlencode(
        {
            "client_id": env["YOUTUBE_CLIENT_ID"],
            "client_secret": env["YOUTUBE_CLIENT_SECRET"],
            "refresh_token": env["YOUTUBE_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        }
    ).encode()
    data = request_json("POST", "https://oauth2.googleapis.com/token", {"Content-Type": "application/x-www-form-urlencoded"}, form)
    return data["access_token"]


def upload_youtube(item: dict, token: str) -> dict:
    path = Path(item["path"])
    size = path.stat().st_size
    meta = {
        "snippet": {
            "title": item["youtube"]["title"],
            "description": item["youtube"]["description"],
            "tags": item["youtube"]["tags"],
            "categoryId": "22",
        },
        "status": {"privacyStatus": item["youtube"]["privacy"], "selfDeclaredMadeForKids": False},
    }
    _, headers, _ = request(
        "POST",
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(size),
        },
        json.dumps(meta).encode(),
    )
    session = headers.get("location")
    if not session:
        raise RuntimeError("YouTube did not return an upload session URL")
    video = request_json("PUT", session, {"Authorization": f"Bearer {token}", "Content-Type": "video/mp4", "Content-Length": str(size)}, path.read_bytes())
    vid = video["id"]
    return {"id": vid, "url": f"https://youtube.com/shorts/{vid}", "privacy": item["youtube"]["privacy"]}


# --------------------------------------------------------------------------- Instagram


def upload_instagram(item: dict) -> dict:
    env = require_env("IG_USER_ID", "IG_ACCESS_TOKEN")
    version = os.environ.get("IG_GRAPH_VERSION", "v23.0")
    token, user = env["IG_ACCESS_TOKEN"], env["IG_USER_ID"]
    path = Path(item["path"])
    size = path.stat().st_size

    container = request_json(
        "POST",
        f"{GRAPH}/{version}/{user}/media?"
        + urllib.parse.urlencode(
            {
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": item["instagram"]["caption"],
                "share_to_feed": "true",
                "access_token": token,
            }
        ),
    )
    cid, upload_uri = container["id"], container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{version}/{container['id']}"
    request("POST", upload_uri, {"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)}, path.read_bytes())

    waited = 0
    while True:
        status = request_json("GET", f"{GRAPH}/{version}/{cid}?" + urllib.parse.urlencode({"fields": "status_code,status", "access_token": token}))
        code = status.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram could not process the video: {status.get('status', code)}")
        if waited >= IG_POLL_MAX_S:
            raise RuntimeError("Instagram is still processing the video after 10 minutes; try `run` again later")
        time.sleep(IG_POLL_S)
        waited += IG_POLL_S

    media = request_json("POST", f"{GRAPH}/{version}/{user}/media_publish?" + urllib.parse.urlencode({"creation_id": cid, "access_token": token}))
    mid = media["id"]
    try:
        link = request_json("GET", f"{GRAPH}/{version}/{mid}?" + urllib.parse.urlencode({"fields": "permalink", "access_token": token})).get("permalink")
    except HttpError:
        link = None
    return {"id": mid, "url": link}


# --------------------------------------------------------------------------- plan


def hashtags(text: str) -> list[str]:
    return [t.lstrip("#") for t in re.findall(r"#\w+", text)]


def with_credits(text: str, credits: list[str]) -> str:
    return text + ("\n\n" + " · ".join(credits) if credits else "")


def build_plan(manifest: dict, platforms: list[str], privacy: str, only: set[int] | None) -> dict:
    items = []
    for clip in manifest.get("clips", []):
        if only and clip["rank"] not in only:
            continue
        path = CLIPS_DIR / clip["file"]
        if not path.exists():
            continue
        credits = sorted({b["credit"] for b in clip.get("broll", []) if b.get("credit")})
        title = (clip.get("youtube_shorts", {}).get("title") or clip["hook_text"])[:100]
        tiktok = clip.get("tiktok", {}).get("description", "")
        item = {"rank": clip["rank"], "file": clip["file"], "path": str(path)}
        if "youtube" in platforms:
            description = with_credits(f"{clip['hook_text']}\n\n{tiktok}\n\n#Shorts".strip(), credits)
            tags = list(dict.fromkeys(["Shorts", *hashtags(tiktok)]))[:15]
            item["youtube"] = {"title": title, "description": description[:5000], "tags": tags, "privacy": privacy}
        if "instagram" in platforms:
            caption = with_credits(clip.get("instagram_reels", {}).get("caption") or clip["hook_text"], credits)
            item["instagram"] = {"caption": caption[:2200], "visibility": "public"}
        items.append(item)
    return {"platforms": platforms, "items": items}


def confirmation_code(plan: dict) -> str:
    """Hash of what will be posted, including each file's size and mtime."""
    files = {it["file"]: [Path(it["path"]).stat().st_size, int(Path(it["path"]).stat().st_mtime)] for it in plan["items"]}
    payload = json.dumps({"plan": {k: v for k, v in plan.items() if k != "confirmation_code"}, "files": files}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:8]


def print_plan(plan: dict) -> None:
    print(f"\nUpload plan ({len(plan['items'])} clip(s)) → {', '.join(plan['platforms'])}\n")
    for it in plan["items"]:
        print(f"  #{it['rank']}  {it['file']}")
        if "youtube" in it:
            y = it["youtube"]
            print(f"      YouTube Shorts [{y['privacy']}]: {y['title']}")
        if "instagram" in it:
            cap = it["instagram"]["caption"].replace("\n", " ")
            print(f"      Instagram Reel [PUBLIC]: {cap[:90]}{'…' if len(cap) > 90 else ''}")
    print(f"\nFull details: {rel(PLAN_JSON)}")
    print(f"To upload exactly this, run:  python3 scripts/upload.py run --confirm {plan['confirmation_code']}\n")


# --------------------------------------------------------------------------- main


def cmd_plan(args) -> None:
    manifest_path = CLIPS_DIR / "metadata.json"
    if not manifest_path.exists():
        sys.exit("No rendered clips found (output/clips/metadata.json). Render first.")
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
    bad = [p for p in platforms if p not in PLATFORMS]
    if bad or not platforms:
        sys.exit(f"--platforms must be a comma list of: {', '.join(PLATFORMS)}")
    only = {int(x) for x in args.only.split(",")} if args.only else None
    plan = build_plan(read_json(manifest_path), platforms, args.privacy, only)
    if not plan["items"]:
        sys.exit("Nothing to upload: no rendered clip files match.")
    plan["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    plan["confirmation_code"] = confirmation_code(plan)
    write_json(PLAN_JSON, plan)
    print_plan(plan)


def cmd_run(args) -> None:
    if not PLAN_JSON.exists():
        sys.exit("No upload plan. Run `upload.py plan` first and review it.")
    plan = read_json(PLAN_JSON)
    expected = confirmation_code(plan)
    if plan.get("confirmation_code") != expected:
        sys.exit("The plan or the clip files changed since it was made. Run `upload.py plan` again and review it.")
    if args.confirm != expected:
        sys.exit("Confirmation code does not match this plan. Review it with `upload.py plan` and pass the code it prints.")

    uploads = read_json(UPLOADS_JSON) if UPLOADS_JSON.exists() else {}
    yt_token = None
    failures = 0
    for it in plan["items"]:
        done = uploads.setdefault(it["file"], {})
        for platform in plan["platforms"]:
            if platform in done and not args.force:
                log(f"#{it['rank']} {platform}: already uploaded ({done[platform].get('url')}); skipping.")
                continue
            try:
                log(f"#{it['rank']} {platform}: uploading {it['file']}…")
                if platform == "youtube":
                    yt_token = yt_token or youtube_access_token()
                    result = upload_youtube(it, yt_token)
                else:
                    result = upload_instagram(it)
            except (RuntimeError, HttpError, urllib.error.URLError, KeyError) as e:
                failures += 1
                log(f"#{it['rank']} {platform}: FAILED — {e}")
                continue
            done[platform] = {**result, "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            write_json(UPLOADS_JSON, uploads)
            log(f"#{it['rank']} {platform}: done → {result.get('url') or result['id']}")
    write_json(UPLOADS_JSON, uploads)
    if failures:
        sys.exit(f"{failures} upload(s) failed; fix the cause and re-run (finished uploads are skipped).")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    plan = sub.add_parser("plan", help="Write and show the upload plan")
    plan.add_argument("--platforms", default="youtube", help="Comma list: youtube,instagram (default youtube)")
    plan.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private", help="YouTube privacy (default private)")
    plan.add_argument("--only", default=None, help="Comma-separated clip ranks")
    run = sub.add_parser("run", help="Upload the reviewed plan")
    run.add_argument("--confirm", required=True, help="The confirmation code printed by `plan`")
    run.add_argument("--force", action="store_true", help="Re-upload clips already recorded in uploads.json")
    args = p.parse_args()
    (cmd_plan if args.cmd == "plan" else cmd_run)(args)


if __name__ == "__main__":
    main()
