#!/usr/bin/env python3
"""Phase 5b — Music bed with ducking, then loudness mastering.

Runs on rendered clips (04_render.ts calls it after each render), replacing
the clip's audio track in place; video is stream-copied.

Without music: two-pass loudnorm of the clip to -14 LUFS / -1.5 dBTP.

With --music FILE|DIR:
  1. The track is looped/trimmed to the clip, faded in/out, and set to
     --music-lufs (default -20 LUFS, i.e. 6 LU under the speech).
  2. The speech drives FFmpeg's `sidechaincompress` on the music. The
     threshold is set from the clip's own speech loudness so that, at a
     typical speaking level, the music ducks by --duck-db (default 12 dB).
  3. Speech + ducked music are mixed and mastered to -14 LUFS (two-pass loudnorm).

With a directory, clip N uses the Nth audio file (sorted), cycling.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common import (
    LOUDNORM_MEASURE,
    TARGET_LUFS,
    WORK_DIR,
    ffmpeg_cmd,
    log,
    loudnorm_filter,
    measure_loudness,
    parse_loudnorm,
    probe,
    read_json,
    run,
    write_json,
)

MIX_FILTERS = ("aresample", "volume", "afade", "asplit", "sidechaincompress", "amix", "loudnorm")
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}
RATIO = 8.0
DETECTOR_OFFSET_DB = 3.0
ATTACK_MS = 15
RELEASE_MS = 350
FADE_IN_S = 0.5
FADE_OUT_S = 1.0
MIN_THRESHOLD = 0.000976563  # sidechaincompress's lower bound (-60 dBFS)


def pick_music(music: Path, rank: int) -> Path:
    if music.is_file():
        return music
    tracks = sorted(p for p in music.iterdir() if p.suffix.lower() in AUDIO_EXTENSIONS)
    if not tracks:
        sys.exit(f"No audio files ({', '.join(sorted(AUDIO_EXTENSIONS))}) in {music}")
    return tracks[(rank - 1) % len(tracks)]


def music_loudness(track: Path) -> float:
    """Integrated loudness of a music file, cached by path + mtime."""
    cache_path = WORK_DIR / "music_loudness.json"
    cache = read_json(cache_path) if cache_path.exists() else {}
    key = f"{track.resolve()}:{int(track.stat().st_mtime)}"
    if key not in cache:
        cache[key] = float(measure_loudness(track)["input_i"])
        write_json(cache_path, cache)
    return cache[key]


def duck_threshold(speech_lufs: float, duck_db: float) -> float:
    """Linear threshold so speech at `speech_lufs` pulls the music down by `duck_db`.

    A compressor reduces gain by (level - threshold) * (1 - 1/ratio), so the
    threshold sits duck_db / (1 - 1/ratio) below the speech level. FFmpeg's
    detector reads ~3 dB above RMS (a sqrt(2) peak-equivalent), measured
    against a steady tone; DETECTOR_OFFSET_DB corrects for it.
    """
    threshold_db = speech_lufs + DETECTOR_OFFSET_DB - duck_db / (1 - 1 / RATIO)
    return max(MIN_THRESHOLD, 10 ** (threshold_db / 20))


def music_graph(duration: float, music_gain_db: float, threshold: float) -> str:
    fade_out_start = max(0.0, duration - FADE_OUT_S)
    return (
        f"[1:a]atrim=duration={duration:.3f},aresample=48000,volume={music_gain_db:.2f}dB,"
        f"afade=t=in:d={FADE_IN_S},afade=t=out:st={fade_out_start:.3f}:d={FADE_OUT_S}[bed];"
        f"[0:a]aresample=48000,asplit=2[voice][key];"
        f"[bed][key]sidechaincompress=threshold={threshold:.6f}:ratio={RATIO}:attack={ATTACK_MS}:release={RELEASE_MS}[ducked];"
        f"[voice][ducked]amix=inputs=2:duration=first:normalize=0[mix]"
    )


def master(clip: Path, music: Path | None, rank: int, music_lufs: float, duck_db: float) -> dict:
    info = probe(clip)
    duration = float(info["format"]["duration"])
    if not any(s["codec_type"] == "audio" for s in info["streams"]):
        raise RuntimeError(f"{clip} has no audio stream")

    speech = measure_loudness(clip)
    inputs = ["-i", str(clip)]
    report: dict = {"speech_lufs": float(speech["input_i"])}

    if music is None:
        graph = None
        m = speech
    else:
        track = pick_music(music, rank)
        gain = music_lufs - music_loudness(track)
        threshold = duck_threshold(float(speech["input_i"]), duck_db)
        graph = music_graph(duration, gain, threshold)
        inputs += ["-stream_loop", "-1", "-i", str(track)]
        # Pass one on the mix itself, so the master hits the target with the music in.
        measure = run(
            ffmpeg_cmd(MIX_FILTERS) + ["-hide_banner", "-nostats"] + inputs
            + ["-filter_complex", graph + ";[mix]" + LOUDNORM_MEASURE, "-f", "null", "-"]
        )
        m = parse_loudnorm(measure.stderr)
        report.update({"music": str(track), "music_gain_db": round(gain, 2), "duck_db": duck_db, "duck_threshold": threshold})

    tmp = clip.with_name(clip.stem + ".mastering.mp4")
    common = ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(tmp)]
    if graph is None:
        run(ffmpeg_cmd() + ["-hide_banner", "-y"] + inputs + ["-map", "0:v:0", "-map", "0:a:0", "-af", loudnorm_filter(m) + ",aresample=48000"] + common)
    else:
        run(
            ffmpeg_cmd(MIX_FILTERS) + ["-hide_banner", "-y"] + inputs
            + ["-filter_complex", graph + f";[mix]{loudnorm_filter(m)},aresample=48000[out]", "-map", "0:v:0", "-map", "[out]"]
            + common
        )
    tmp.replace(clip)
    report.update({"mix_lufs_before": float(m["input_i"]), "target_lufs": TARGET_LUFS})
    return report


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--clip", action="append", required=True, help="Rendered clip to master in place (repeatable)")
    p.add_argument("--rank", type=int, action="append", help="Clip rank per --clip (picks the music track from a directory)")
    p.add_argument("--music", type=Path, default=None, help="Music file, or a directory of tracks")
    p.add_argument("--music-lufs", type=float, default=-20.0, help="Music bed loudness before ducking (default -20)")
    p.add_argument("--duck-db", type=float, default=12.0, help="How far the music drops under speech (default 12 dB)")
    p.add_argument("--report", type=Path, default=None, help="Write a JSON report of what was done")
    args = p.parse_args()

    if args.music and not args.music.exists():
        sys.exit(f"Music not found: {args.music}")
    ranks = args.rank or []
    reports = {}
    for i, clip in enumerate(args.clip):
        path = Path(clip)
        rank = ranks[i] if i < len(ranks) else i + 1
        reports[path.name] = master(path, args.music, rank, args.music_lufs, args.duck_db)
        r = reports[path.name]
        extra = f", music {Path(r['music']).name} ducked {args.duck_db:g} dB" if "music" in r else ""
        log(f"Mastered {path.name} to {TARGET_LUFS:g} LUFS{extra}.")
    if args.report:
        write_json(args.report, reports)


if __name__ == "__main__":
    main()
