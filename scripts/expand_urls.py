#!/usr/bin/env python3
"""Expand batch inputs into a flat list of videos, as JSON on stdout.

Each input may be a YouTube video, a playlist or a channel URL (expanded to
its videos, newest first, up to --limit each), or a local video file path.

  python3 scripts/expand_urls.py --limit 10 URL_OR_PATH [...]
  -> [{"source": "url"|"file", "url"/"path": ..., "id": ..., "title": ...}, ...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

LOCAL_EXTENSIONS = {".mp4", ".mov", ".mkv"}


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-")
    return s or hashlib.sha1(text.encode()).hexdigest()[:10]


def expand_url(url: str, limit: int) -> list[dict]:
    import yt_dlp

    opts = {"extract_flat": "in_playlist", "quiet": True, "skip_download": True, "playlistend": limit or None}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    def entries(node: dict):
        # Channels nest tabs (Videos, Shorts, ...) as playlists of playlists.
        for e in node.get("entries") or []:
            if e is None:
                continue
            if e.get("_type") == "playlist" or (e.get("entries") is not None):
                yield from entries(e)
            else:
                yield e

    if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
        out = []
        for e in entries(info):
            vid = e.get("id")
            link = e.get("url") if str(e.get("url", "")).startswith("http") else f"https://www.youtube.com/watch?v={vid}"
            out.append({"source": "url", "url": link, "id": vid, "title": e.get("title")})
            if limit and len(out) >= limit:
                break
        return out
    return [{"source": "url", "url": info.get("webpage_url", url), "id": info.get("id"), "title": info.get("title")}]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inputs", nargs="+")
    p.add_argument("--limit", type=int, default=0, help="Max videos per playlist/channel (0 = all)")
    args = p.parse_args()

    videos: list[dict] = []
    seen: set[str] = set()
    for item in args.inputs:
        item = item.strip()
        if not item or item.startswith("#"):
            continue
        if not re.match(r"^https?://", item):
            path = Path(item).expanduser()
            if path.suffix.lower() not in LOCAL_EXTENSIONS or not path.exists():
                print(f"[viral-clipper] skipping {item}: not a URL or an existing .mp4/.mov/.mkv", file=sys.stderr)
                continue
            found = [{"source": "file", "path": str(path.resolve()), "id": slug(path.stem), "title": path.stem}]
        else:
            try:
                found = expand_url(item, args.limit)
            except Exception as e:  # one bad URL shouldn't sink the batch
                print(f"[viral-clipper] could not read {item}: {e}", file=sys.stderr)
                found = [{"source": "url", "url": item, "id": slug(item), "title": None, "error": str(e)}]
        for v in found:
            key = v.get("url") or v.get("path")
            if key in seen:
                continue
            seen.add(key)
            raw = str(v.get("id") or "")
            # YouTube ids are case-sensitive and already filename-safe; keep them as-is.
            v["id"] = raw if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw) else slug(raw or key)
            videos.append(v)
    json.dump(videos, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
