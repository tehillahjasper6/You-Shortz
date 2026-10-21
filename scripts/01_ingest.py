#!/usr/bin/env python3
"""Phase 1a — Ingestion.

Downloads a YouTube video (<=1080p MP4 + subtitles when available) or imports
a local .mp4/.mov/.mkv, then writes:

  public/source.mp4     Browser-friendly H.264/AAC source, audio normalized
                        to -14 LUFS (two-pass loudnorm). Never overwritten
                        unless --force is given.
  output/audio.wav      16 kHz mono speech track for transcription.
  output/ingest.json    What was ingested, from where, and its properties.

Re-running with the same source is a no-op, so later phases can be re-run
without re-downloading.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from common import (
    AUDIO_WAV,
    HIGHLIGHTS_JSON,
    INGEST_JSON,
    SOURCE_VIDEO,
    TARGET_LRA,
    TARGET_LUFS,
    TARGET_TRUE_PEAK,
    TRANSCRIPT_JSON,
    WORK_DIR,
    ffmpeg_cmd,
    ffmpeg_dir_for_ytdlp,
    log,
    probe,
    read_json,
    run,
    write_json,
)

LOCAL_EXTENSIONS = {".mp4", ".mov", ".mkv"}
MAX_HEIGHT = 1080


def source_key(args) -> dict:
    if args.url:
        return {"type": "youtube", "url": args.url}
    path = Path(args.file).resolve()
    stat = path.stat()
    return {"type": "file", "path": str(path), "size": stat.st_size, "mtime": int(stat.st_mtime)}


def download_youtube(url: str, sub_lang: str, dest: Path) -> tuple[Path, dict]:
    try:
        import yt_dlp
    except ImportError:
        sys.exit("yt-dlp is not installed. Run: pip install -r requirements.txt")

    opts = {
        # Highest resolution <=1080p, preferring H.264 in MP4 so the source can be stream-copied.
        "format": (
            f"bv*[height<={MAX_HEIGHT}][vcodec^=avc1][ext=mp4]+ba[ext=m4a]/"
            f"bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]/b"
        ),
        "merge_output_format": "mp4",
        "outtmpl": str(dest / "original.%(ext)s"),
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [sub_lang, f"{sub_lang}-orig"],
        "subtitlesformat": "json3",
        "noplaylist": True,
        "quiet": False,
        "noprogress": True,
    }
    ffmpeg_dir = ffmpeg_dir_for_ytdlp()
    if ffmpeg_dir:
        opts["ffmpeg_location"] = ffmpeg_dir

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        video_path = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
    if not video_path.exists():
        candidates = [p for p in dest.glob("original.*") if p.suffix in LOCAL_EXTENSIONS | {".webm"}]
        if not candidates:
            raise RuntimeError("yt-dlp finished but no video file was found")
        video_path = candidates[0]

    subs = None
    for lang, sub in (info.get("requested_subtitles") or {}).items():
        path = sub.get("filepath")
        if path and Path(path).exists():
            manual = lang in (info.get("subtitles") or {})
            subs = {"path": str(path), "lang": lang, "kind": "manual" if manual else "auto"}
            break

    meta = {
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "webpage_url": info.get("webpage_url", url),
        "subtitles": subs,
    }
    return video_path, meta


def measure_loudness(path: Path) -> dict:
    """First loudnorm pass: measure integrated loudness, true peak and LRA."""
    af = f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK}:LRA={TARGET_LRA}:print_format=json"
    proc = run(ffmpeg_cmd() + ["-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", af, "-f", "null", "-"])
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.S)
    if not match:
        raise RuntimeError("Could not parse loudnorm measurement output")
    return json.loads(match.group(0))


def build_source(original: Path, out: Path) -> dict:
    """Create public/source.mp4: H.264 video (copied when possible) + loudness-normalized AAC."""
    info = probe(original)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if video is None:
        raise RuntimeError(f"{original} has no video stream")
    if audio is None:
        raise RuntimeError(f"{original} has no audio stream — nothing to transcribe")

    height = int(video.get("height", 0))
    can_copy = (
        video.get("codec_name") == "h264"
        and video.get("pix_fmt") in ("yuv420p", "yuvj420p")
        and height <= MAX_HEIGHT
    )

    log("Measuring loudness (pass 1/2)…")
    m = measure_loudness(original)
    af = (
        f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK}:LRA={TARGET_LRA}"
        f":measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
        f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true"
    )

    if can_copy:
        vargs = ["-c:v", "copy"]
    else:
        log(f"Re-encoding video ({video.get('codec_name')} {height}p) to H.264 ≤{MAX_HEIGHT}p…")
        vargs = [
            "-vf", f"scale=-2:'min({MAX_HEIGHT},ih)'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        ]

    tmp = out.with_name(out.stem + ".partial.mp4")
    log("Normalizing audio to -14 LUFS (pass 2/2) and writing source…")
    run(
        ffmpeg_cmd()
        + ["-hide_banner", "-y", "-i", str(original), "-map", "0:v:0", "-map", "0:a:0"]
        + vargs
        + ["-af", af, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(tmp)]
    )
    tmp.replace(out)
    return {
        "input_lufs": float(m["input_i"]),
        "target_lufs": TARGET_LUFS,
        "video_stream_copied": can_copy,
    }


def extract_speech_audio(source: Path, out: Path) -> None:
    run(
        ffmpeg_cmd()
        + ["-hide_banner", "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)]
    )


def video_props(path: Path) -> dict:
    info = probe(path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    num, den = (v.get("avg_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    return {
        "duration": float(info["format"]["duration"]),
        "width": int(v["width"]),
        "height": int(v["height"]),
        "fps": round(fps, 3),
        "video_codec": v.get("codec_name"),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="YouTube URL")
    src.add_argument("--file", help="Local .mp4 / .mov / .mkv")
    p.add_argument("--sub-lang", default="en", help="Subtitle language to fetch from YouTube (default: en)")
    p.add_argument("--force", action="store_true", help="Replace an existing public/source.mp4 from a different source")
    args = p.parse_args()

    if args.file:
        f = Path(args.file)
        if not f.exists():
            sys.exit(f"File not found: {f}")
        if f.suffix.lower() not in LOCAL_EXTENSIONS:
            sys.exit(f"Unsupported file type {f.suffix}; expected one of {sorted(LOCAL_EXTENSIONS)}")

    key = source_key(args)
    if SOURCE_VIDEO.exists() and INGEST_JSON.exists():
        previous = read_json(INGEST_JSON)
        if previous.get("source") == key:
            if not AUDIO_WAV.exists():
                extract_speech_audio(SOURCE_VIDEO, AUDIO_WAV)
            log(f"Already ingested ({SOURCE_VIDEO.relative_to(SOURCE_VIDEO.parent.parent)}); skipping download.")
            return
        if not args.force:
            sys.exit(
                "public/source.mp4 already holds a different source. "
                "Re-run with --force to replace it (this also invalidates transcript and highlights)."
            )
    elif SOURCE_VIDEO.exists() and not args.force:
        sys.exit("public/source.mp4 exists but output/ingest.json does not. Re-run with --force to replace it.")

    # A new source invalidates everything derived from the old one. Keep the old
    # artifacts around (highlights may hold hand edits) rather than deleting them.
    for stale in (TRANSCRIPT_JSON, HIGHLIGHTS_JSON):
        if stale.exists():
            backup = stale.with_suffix(".stale.json")
            stale.replace(backup)
            log(f"Moved outdated {stale.name} to {backup.name}")

    dl_dir = WORK_DIR / "download"
    if dl_dir.exists():
        shutil.rmtree(dl_dir)
    dl_dir.mkdir(parents=True)

    meta: dict = {"title": None, "subtitles": None}
    if args.url:
        log(f"Downloading {args.url}…")
        original, meta = download_youtube(args.url, args.sub_lang, dl_dir)
    else:
        original = Path(args.file).resolve()
        meta["title"] = original.stem

    SOURCE_VIDEO.parent.mkdir(parents=True, exist_ok=True)
    loudness = build_source(original, SOURCE_VIDEO)
    log("Extracting 16 kHz speech track…")
    extract_speech_audio(SOURCE_VIDEO, AUDIO_WAV)

    subtitles = meta.get("subtitles")
    if subtitles:
        kept = WORK_DIR / f"subtitles.{subtitles['lang']}.json3"
        shutil.copyfile(subtitles["path"], kept)
        subtitles = {**subtitles, "path": str(kept.relative_to(WORK_DIR.parent.parent))}

    record = {
        "source": key,
        "title": meta.get("title"),
        "uploader": meta.get("uploader"),
        "webpage_url": meta.get("webpage_url"),
        "video": "public/source.mp4",
        **video_props(SOURCE_VIDEO),
        "audio": {**loudness, "speech_track": "output/audio.wav"},
        "subtitles": subtitles,
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_json(INGEST_JSON, record)

    shutil.rmtree(dl_dir, ignore_errors=True)
    log(f"Ingested “{record['title']}” ({record['duration']:.1f}s, {record['width']}x{record['height']}).")


if __name__ == "__main__":
    main()
