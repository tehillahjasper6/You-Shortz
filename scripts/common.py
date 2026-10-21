"""Shared helpers for the Python pipeline phases.

Paths, FFmpeg/FFprobe resolution and small JSON utilities. FFmpeg is resolved
in this order: $FFMPEG / $FFPROBE, Remotion's bundled binaries
(`npx remotion ffmpeg`), then whatever is on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = ROOT / "public"
OUTPUT_DIR = ROOT / "output"
WORK_DIR = OUTPUT_DIR / "work"
CLIPS_DIR = OUTPUT_DIR / "clips"

SOURCE_VIDEO = PUBLIC_DIR / "source.mp4"
INGEST_JSON = OUTPUT_DIR / "ingest.json"
AUDIO_WAV = OUTPUT_DIR / "audio.wav"
TRANSCRIPT_JSON = OUTPUT_DIR / "transcript.json"
HIGHLIGHTS_JSON = OUTPUT_DIR / "highlights.json"

TARGET_LUFS = -14.0
TARGET_TRUE_PEAK = -1.5
TARGET_LRA = 11.0


def log(msg: str) -> None:
    print(f"[viral-clipper] {msg}", file=sys.stderr, flush=True)


def _tool(name: str) -> list[str]:
    env = os.environ.get(name.upper())
    if env:
        return [env]
    remotion_bin = ROOT / "node_modules" / ".bin" / ("remotion.cmd" if os.name == "nt" else "remotion")
    if remotion_bin.exists():
        return [str(remotion_bin), name]
    system = shutil.which(name)
    if system:
        return [system]
    raise RuntimeError(
        f"Could not find {name}. Run `npm install` (Remotion bundles FFmpeg) or set ${name.upper()}."
    )


def ffmpeg_dir_for_ytdlp() -> str | None:
    """A directory containing ffmpeg + ffprobe binaries, for yt-dlp's stream merging.

    Prefers a system install; otherwise points at Remotion's compositor package,
    whose binaries need their shared libraries on the loader path.
    """
    system = shutil.which("ffmpeg")
    if system and shutil.which("ffprobe"):
        return str(Path(system).parent)
    for pkg in sorted((ROOT / "node_modules" / "@remotion").glob("compositor-*")):
        if (pkg / "ffmpeg").exists() or (pkg / "ffmpeg.exe").exists():
            if sys.platform.startswith("linux"):
                os.environ["LD_LIBRARY_PATH"] = f"{pkg}:{os.environ.get('LD_LIBRARY_PATH', '')}"
            elif sys.platform == "darwin":
                os.environ["DYLD_LIBRARY_PATH"] = f"{pkg}:{os.environ.get('DYLD_LIBRARY_PATH', '')}"
            return str(pkg)
    return None


def ffmpeg_cmd() -> list[str]:
    return _tool("ffmpeg")


def ffprobe_cmd() -> list[str]:
    return _tool("ffprobe")


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a command, raising with the tail of stderr attached on failure."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr[-4000:]}")
    return proc


def probe(path: Path) -> dict:
    proc = run(
        ffprobe_cmd()
        + ["-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    )
    return json.loads(proc.stdout)


def media_duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def read_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)
