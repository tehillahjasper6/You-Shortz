#!/usr/bin/env python3
"""Phase 4b — Contextual B-roll.

Finds concrete, filmable things a clip mentions ("iPhone 16", "Mount Fuji",
"a cup of coffee") and fetches matching stock footage. The render cuts away to
it for ~1.5 s at the moment the word is spoken, with a cross-dissolve, while
the original audio and captions continue.

Picking moments:
  anthropic / gemini  The LLM picks up to --per-clip moments per clip and
                      writes a stock-search query for each.
  heuristic           Multi-word proper nouns and capitalised names (offline).

Footage:
  Pexels videos   (PEXELS_API_KEY)        preferred
  Unsplash photos (UNSPLASH_ACCESS_KEY)   fallback; rendered with a slow zoom

Media is cached in public/broll/ and the plan goes to output/broll.json, which
you can edit (move `at`, change `query`, delete items) before rendering.
Entries are reused while a clip's start/end are unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse

from common import (
    BROLL_DIR,
    BROLL_JSON,
    HIGHLIGHTS_JSON,
    TRANSCRIPT_JSON,
    download,
    http_json,
    log,
    read_json,
    rel,
    write_json,
)

BROLL_VERSION = 1
CUTAWAY_S = 1.5
SKIP_START_S = 3.0  # keep the hook and the speaker's face on screen at the start…
SKIP_END_S = 2.0  # …and at the end, so the loop point shows the speaker
MIN_GAP_S = 4.0  # between cutaways

DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "gemini": "gemini-2.5-flash"}

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "at": {"type": "number", "description": "Absolute start time (seconds) of the word that names the thing"},
                    "phrase": {"type": "string", "description": "The words as spoken"},
                    "query": {"type": "string", "description": "2-4 word stock-footage search query"},
                },
                "required": ["at", "phrase", "query"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}

PICK_PROMPT = """You choose B-roll cutaways for a vertical short video.

Pick up to {n} moments where the speaker names something concrete and visual that stock footage can show: a product, place, object, animal, food, activity or recognisable setting. Skip abstract ideas, feelings, people's names and anything a generic stock clip would misrepresent.

For each moment give:
- at: the absolute start time, in seconds, of the first word of the phrase, copied from the transcript's word<seconds> tags. It must be between {lo:.2f} and {hi:.2f}.
- phrase: the words as spoken.
- query: a 2-4 word stock-footage search query that would find that visual.

Moments must be at least {gap:.0f} seconds apart. Return fewer (or none) rather than weak picks.

<transcript>
{transcript}
</transcript>"""

STOP_CAPS = {"I", "I'm", "I've", "I'll", "I'd", "OK", "Okay", "So", "And", "But", "The", "A", "An", "It", "This", "That", "We", "You"}


def clip_words(transcript: dict, start: float, end: float) -> list[dict]:
    return [w for s in transcript["segments"] for w in s["words"] if w["start"] >= start and w["end"] <= end]


# --------------------------------------------------------------------------- picking


def pick_heuristic(words: list[dict], lo: float, hi: float, n: int) -> list[dict]:
    """Runs of capitalised words (not sentence-initial), optionally followed by a number."""
    picks, i = [], 0
    while i < len(words):
        w = words[i]
        token = w["word"].strip(".,!?;:\"'()")
        prev = words[i - 1]["word"] if i else "."
        sentence_start = i == 0 or prev.endswith((".", "!", "?"))
        if token[:1].isupper() and token not in STOP_CAPS and not sentence_start:
            j = i
            parts = [token]
            while j + 1 < len(words):
                nxt = words[j + 1]["word"].strip(".,!?;:\"'()")
                if nxt[:1].isupper() or nxt.isdigit():
                    parts.append(nxt)
                    j += 1
                else:
                    break
            if lo <= w["start"] <= hi:
                phrase = " ".join(parts)
                picks.append({"at": w["start"], "phrase": phrase, "query": phrase})
            i = j + 1
        else:
            i += 1
    # Longer names first (more specific), then keep spacing.
    picks.sort(key=lambda p: -len(p["phrase"]))
    return space_out(picks, n)


def space_out(picks: list[dict], n: int) -> list[dict]:
    kept: list[dict] = []
    for p in picks:
        if all(abs(p["at"] - k["at"]) >= MIN_GAP_S for k in kept):
            kept.append(p)
        if len(kept) == n:
            break
    return sorted(kept, key=lambda p: p["at"])


def pick_llm(provider: str, model: str, words: list[dict], lo: float, hi: float, n: int) -> list[dict]:
    transcript = " ".join(f"{w['word']}<{w['start']:.2f}>" for w in words)
    prompt = PICK_PROMPT.format(n=n, lo=lo, hi=hi, gap=MIN_GAP_S, transcript=transcript)
    if provider == "anthropic":
        import anthropic

        client = anthropic.Anthropic()
        response = client.beta.messages.create(
            model=model,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": PICK_SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise ValueError(f"model stopped with {response.stop_reason}")
        data = json.loads(next(b.text for b in response.content if b.type == "text"))
    else:
        from google import genai

        response = genai.Client().models.generate_content(
            model=model,
            contents=prompt,
            config={"response_mime_type": "application/json", "response_json_schema": PICK_SCHEMA},
        )
        data = json.loads(response.text)

    picks = []
    for m in data.get("moments", []):
        at = m.get("at")
        if not isinstance(at, (int, float)) or not lo <= at <= hi or not str(m.get("query", "")).strip():
            continue
        # Snap to the nearest spoken word so the cutaway lands on the word itself.
        nearest = min(words, key=lambda w: abs(w["start"] - at))
        picks.append({"at": nearest["start"], "phrase": m.get("phrase", ""), "query": m["query"].strip()})
    return space_out(picks, n)


# --------------------------------------------------------------------------- fetching


def _cache_name(prefix: str, ident: str, ext: str) -> str:
    return f"{prefix}_{hashlib.sha1(ident.encode()).hexdigest()[:12]}.{ext}"


def fetch_pexels(query: str, key: str, exclude: set[str]) -> dict | None:
    url = "https://api.pexels.com/videos/search?" + urllib.parse.urlencode(
        {"query": query, "orientation": "portrait", "size": "medium", "per_page": 8}
    )
    data = http_json(url, {"Authorization": key})
    for video in data.get("videos", []):
        if str(video["id"]) in exclude or video.get("duration", 0) < CUTAWAY_S:
            continue
        files = [
            f for f in video.get("video_files", [])
            if f.get("file_type") == "video/mp4" and (f.get("height") or 0) >= 1080 and (f.get("width") or 0) <= (f.get("height") or 0)
        ]
        files = files or [f for f in video.get("video_files", []) if f.get("file_type") == "video/mp4" and (f.get("height") or 0) >= 720]
        if not files:
            continue
        best = min(files, key=lambda f: f.get("height") or 0)  # smallest file that is still ≥1080p
        name = _cache_name("pexels", str(video["id"]), "mp4")
        dest = BROLL_DIR / name
        if not dest.exists():
            download(best["link"], dest)
        user = (video.get("user") or {}).get("name", "Pexels")
        return {
            "type": "video",
            "file": f"broll/{name}",
            "id": str(video["id"]),
            "credit": f"Video by {user} on Pexels",
            "source_url": video.get("url"),
        }
    return None


def fetch_unsplash(query: str, key: str, exclude: set[str]) -> dict | None:
    url = "https://api.unsplash.com/search/photos?" + urllib.parse.urlencode(
        {"query": query, "orientation": "portrait", "per_page": 8, "content_filter": "high"}
    )
    auth = {"Authorization": f"Client-ID {key}"}
    data = http_json(url, auth)
    for photo in data.get("results", []):
        if photo["id"] in exclude:
            continue
        name = _cache_name("unsplash", photo["id"], "jpg")
        dest = BROLL_DIR / name
        if not dest.exists():
            download(photo["urls"]["raw"] + "&w=1080&h=1920&fit=crop&fm=jpg&q=85", dest)
            # Unsplash API guidelines: register the download.
            try:
                http_json(photo["links"]["download_location"], auth)
            except (urllib.error.URLError, KeyError):
                pass
        user = photo.get("user") or {}
        return {
            "type": "image",
            "file": f"broll/{name}",
            "id": photo["id"],
            "credit": f"Photo by {user.get('name', 'Unsplash')} on Unsplash",
            "source_url": (photo.get("links") or {}).get("html"),
        }
    return None


def fetch_media(query: str, exclude: set[str]) -> dict | None:
    pexels, unsplash = os.environ.get("PEXELS_API_KEY"), os.environ.get("UNSPLASH_ACCESS_KEY")
    for fetch, key in ((fetch_pexels, pexels), (fetch_unsplash, unsplash)):
        if not key:
            continue
        try:
            media = fetch(query, key, exclude)
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
            log(f"    {fetch.__name__.split('_')[1]} lookup for '{query}' failed: {e}")
            continue
        if media:
            return media
    return None


# --------------------------------------------------------------------------- main


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", choices=["anthropic", "gemini", "heuristic"], default="anthropic")
    p.add_argument("--model", default=None)
    p.add_argument("--per-clip", type=int, default=2, help="Maximum cutaways per clip (default 2)")
    p.add_argument("--only", default=None, help="Comma-separated ranks (default: all)")
    p.add_argument("--force", action="store_true", help="Re-pick and re-fetch even when cached")
    args = p.parse_args()

    if not os.environ.get("PEXELS_API_KEY") and not os.environ.get("UNSPLASH_ACCESS_KEY"):
        log("No PEXELS_API_KEY or UNSPLASH_ACCESS_KEY set; skipping B-roll.")
        return
    if not HIGHLIGHTS_JSON.exists() or not TRANSCRIPT_JSON.exists():
        sys.exit("Run phases 1-2 first (highlights.json / transcript.json missing).")

    highlights = read_json(HIGHLIGHTS_JSON)
    transcript = read_json(TRANSCRIPT_JSON)
    only = {int(x) for x in args.only.split(",")} if args.only else None
    plan = read_json(BROLL_JSON) if BROLL_JSON.exists() else {}
    if plan.get("version") != BROLL_VERSION:
        plan = {"version": BROLL_VERSION, "clips": {}}
    model = args.model or DEFAULT_MODELS.get(args.provider, "heuristic")
    used: set[str] = {it["id"] for c in plan["clips"].values() for it in c.get("items", []) if it.get("id")}

    for clip in highlights["clips"]:
        rank, start, end = clip["rank"], clip["start"], clip["end"]
        if only and rank not in only:
            continue
        prev = plan["clips"].get(str(rank))
        if prev and not args.force and prev["start"] == start and prev["end"] == end:
            log(f"#{rank}: B-roll plan up to date ({len(prev['items'])} cutaway(s)).")
            continue

        words = clip_words(transcript, start, end)
        lo, hi = start + SKIP_START_S, end - SKIP_END_S - CUTAWAY_S
        if not words or hi <= lo:
            plan["clips"][str(rank)] = {"start": start, "end": end, "items": []}
            continue
        if args.provider == "heuristic":
            picks = pick_heuristic(words, lo, hi, args.per_clip)
        else:
            try:
                picks = pick_llm(args.provider, model, words, lo, hi, args.per_clip)
            except Exception as e:  # B-roll is optional; never fail the run over it
                log(f"#{rank}: LLM pick failed ({e}); falling back to the heuristic.")
                picks = pick_heuristic(words, lo, hi, args.per_clip)

        items = []
        for pick in picks:
            media = fetch_media(pick["query"], used)
            if not media:
                log(f"#{rank}: no footage for '{pick['query']}'.")
                continue
            used.add(media["id"])
            items.append({"at": round(pick["at"], 3), "duration": CUTAWAY_S, "phrase": pick["phrase"], "query": pick["query"], **media})
            log(f"#{rank}: {pick['at']:.2f}s “{pick['phrase']}” → {media['file']} ({media['credit']})")
        plan["clips"][str(rank)] = {"start": start, "end": end, "items": items}
        write_json(BROLL_JSON, plan)

    write_json(BROLL_JSON, plan)
    log(f"Wrote {rel(BROLL_JSON)}.")


if __name__ == "__main__":
    main()
