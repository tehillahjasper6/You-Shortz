"""Shared helpers for the Python pipeline phases.

Paths, FFmpeg/FFprobe resolution and small JSON utilities. FFmpeg is resolved
in this order: $FFMPEG / $FFPROBE, Remotion's bundled binaries
(`npx remotion ffmpeg`), then whatever is on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# One video's working set (public/ + output/). The repo root by default; batch
# mode gives every video its own directory via $VIRAL_CLIPPER_WORKSPACE.
WORKSPACE = Path(os.environ.get("VIRAL_CLIPPER_WORKSPACE") or ROOT).resolve()
PUBLIC_DIR = WORKSPACE / "public"
OUTPUT_DIR = WORKSPACE / "output"
WORK_DIR = OUTPUT_DIR / "work"
CLIPS_DIR = OUTPUT_DIR / "clips"

SOURCE_VIDEO = PUBLIC_DIR / "source.mp4"
INGEST_JSON = OUTPUT_DIR / "ingest.json"
AUDIO_WAV = OUTPUT_DIR / "audio.wav"
TRANSCRIPT_JSON = OUTPUT_DIR / "transcript.json"
HIGHLIGHTS_JSON = OUTPUT_DIR / "highlights.json"
FACE_TRACKS_JSON = OUTPUT_DIR / "face_tracks.json"
BROLL_JSON = OUTPUT_DIR / "broll.json"
BROLL_DIR = PUBLIC_DIR / "broll"
MODELS_DIR = ROOT / "models"

TARGET_LUFS = -14.0
TARGET_TRUE_PEAK = -1.5
TARGET_LRA = 11.0


def rel(path: Path) -> str:
    """Path for log messages: relative to the repo when inside it."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


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


_FILTER_CACHE: dict[str, set[str]] = {}


def _ffmpeg_candidates() -> list[list[str]]:
    candidates = []
    if os.environ.get("FFMPEG"):
        candidates.append([os.environ["FFMPEG"]])
    remotion_bin = ROOT / "node_modules" / ".bin" / ("remotion.cmd" if os.name == "nt" else "remotion")
    if remotion_bin.exists():
        candidates.append([str(remotion_bin), "ffmpeg"])
    try:  # pip's imageio-ffmpeg ships a full static build
        import imageio_ffmpeg

        candidates.append([imageio_ffmpeg.get_ffmpeg_exe()])
    except Exception:
        pass
    if shutil.which("ffmpeg"):
        candidates.append([shutil.which("ffmpeg")])
    return candidates


def _filters(cmd: list[str]) -> set[str]:
    key = " ".join(cmd)
    if key not in _FILTER_CACHE:
        proc = subprocess.run(cmd + ["-hide_banner", "-filters"], capture_output=True, text=True)
        _FILTER_CACHE[key] = {parts[1] for line in proc.stdout.splitlines() if len(parts := line.split()) > 2}
    return _FILTER_CACHE[key]


def ffmpeg_cmd(filters: tuple[str, ...] = ()) -> list[str]:
    """An FFmpeg command line prefix, choosing a build that has every filter in `filters`.

    Remotion's bundled FFmpeg is trimmed for rendering (no asplit, afade or
    sidechaincompress), so mixing work falls through to imageio-ffmpeg's full
    static build or a system install.
    """
    candidates = _ffmpeg_candidates()
    if not candidates:
        raise RuntimeError("Could not find ffmpeg. Run `npm install` and `pip install -r requirements.txt`, or set $FFMPEG.")
    if not filters:
        return candidates[0]
    for cmd in candidates:
        if set(filters) <= _filters(cmd):
            return cmd
    raise RuntimeError(
        f"No ffmpeg build with filters {', '.join(filters)}. `pip install imageio-ffmpeg` provides one, or set $FFMPEG."
    )


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


LOUDNORM_MEASURE = f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK}:LRA={TARGET_LRA}:print_format=json"


def parse_loudnorm(stderr: str) -> dict:
    """The JSON block loudnorm prints at the end of a measuring pass."""
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.S)
    if not match:
        raise RuntimeError("Could not parse loudnorm measurement output")
    return json.loads(match.group(0))


def measure_loudness(path: Path) -> dict:
    """First loudnorm pass: integrated loudness, true peak and LRA of the first audio stream."""
    proc = run(ffmpeg_cmd() + ["-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", LOUDNORM_MEASURE, "-f", "null", "-"])
    return parse_loudnorm(proc.stderr)


def loudnorm_filter(m: dict) -> str:
    """Second loudnorm pass: linear normalization to TARGET_LUFS using pass-one measurements."""
    return (
        f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK}:LRA={TARGET_LRA}"
        f":measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
        f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true"
    )


def download(url: str, dest: Path, headers: dict | None = None, sha256: str | None = None) -> Path:
    """Fetch url to dest atomically, optionally verifying a SHA-256."""
    import hashlib
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "viral-clipper/0.2", **(headers or {})})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    if sha256:
        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
        if digest != sha256:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"Checksum mismatch for {url}: got {digest}")
    tmp.replace(dest)
    return dest


def http_json(url: str, headers: dict | None = None) -> dict:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "viral-clipper/0.2", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
