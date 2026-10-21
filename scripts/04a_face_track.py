#!/usr/bin/env python3
"""Phase 4a — Face-aware auto-reframing.

For every clip in highlights.json, samples the source at a few frames per
second, detects faces with OpenCV's ResNet-10 SSD face detector, picks the
subject to follow, and smooths that into a virtual-camera path. The render
uses the path to slide a 9:16 crop window across the source, instead of a
static centre crop.

Subject choice: start on the largest face. Then stay on it (nearest
detection to where the camera is) unless another face is clearly bigger for
over a second, which is how a cut to a different speaker usually looks.

Camera: median-filtered target, a dead zone so small head movements don't
move the frame, and a capped pan speed so cuts between speakers glide.

Writes output/face_tracks.json; entries are reused while a clip's start/end
are unchanged, so editing one clip in highlights.json only re-tracks that one.

The model (~5 MB) is downloaded once into models/ and checksum-verified.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

from common import (
    FACE_TRACKS_JSON,
    HIGHLIGHTS_JSON,
    MODELS_DIR,
    ROOT,
    SOURCE_VIDEO,
    download,
    log,
    read_json,
    write_json,
)

MODEL_FILES = {
    "deploy.prototxt": (
        "https://raw.githubusercontent.com/opencv/opencv/4.10.0/samples/dnn/face_detector/deploy.prototxt",
        "dcd661dc48fc9de0a341db1f666a2164ea63a67265c7f779bc12d6b3f2fa67e9",
    ),
    "res10_300x300_ssd_iter_140000_fp16.caffemodel": (
        "https://raw.githubusercontent.com/opencv/opencv_3rdparty/dnn_samples_face_detector_20180205_fp16/res10_300x300_ssd_iter_140000_fp16.caffemodel",
        "510ffd2471bd81e3fcc88a5beb4eae4fb445ccf8333ebc54e7302b83f4158a76",
    ),
}

TRACK_VERSION = 1
SAMPLE_FPS = 6.0
MIN_CONFIDENCE = 0.55
MIN_FACE_WIDTH = 0.03  # fraction of frame width; ignores faces in crowds/posters
MIN_COVERAGE = 0.4  # fraction of samples with a face before tracking is used
SWITCH_AREA_RATIO = 1.5  # another face must be this much bigger…
SWITCH_HOLD_S = 1.0  # …for this long before the camera changes subject
MEDIAN_WINDOW = 5
DEAD_ZONE = 0.04  # fraction of frame width the subject may drift before the camera follows
MAX_PAN_PER_S = 0.45  # fraction of frame width the camera may move per second


def ensure_model() -> tuple[Path, Path]:
    d = MODELS_DIR / "face_detector"
    for name, (url, sha) in MODEL_FILES.items():
        path = d / name
        if not path.exists():
            log(f"Downloading face detector {name}…")
            download(url, path, sha256=sha)
    return d / "deploy.prototxt", d / "res10_300x300_ssd_iter_140000_fp16.caffemodel"


def _detect_square(net, square) -> list[tuple[float, float, float, float, float]]:
    """Run the 300x300 SSD on a square image; boxes as (x1, y1, x2, y2, score) in 0-1."""
    import cv2

    net.setInput(cv2.dnn.blobFromImage(cv2.resize(square, (300, 300)), 1.0, (300, 300), (104.0, 177.0, 123.0)))
    out = net.forward()
    boxes = []
    for i in range(out.shape[2]):
        score = float(out[0, 0, i, 2])
        if score >= MIN_CONFIDENCE:
            x1, y1, x2, y2 = (min(1.0, max(0.0, float(v))) for v in out[0, 0, i, 3:7])
            if x2 > x1 and y2 > y1:
                boxes.append((x1, y1, x2, y2, score))
    return boxes


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def detect_faces(net, frame) -> list[dict]:
    """Faces in a frame, as centres/sizes in 0-1 of the frame.

    The detector sees 300x300 inputs, so squashing a 16:9 frame distorts faces
    and shrinks small ones below what it can find. Instead run it on
    overlapping full-height square tiles (two for 16:9), then merge duplicates.
    """
    h, w = frame.shape[:2]
    side = min(h, w)
    span = max(h, w) - side
    # One extra tile beyond the minimum so a face on a tile seam is whole in some tile (16:9 -> 3).
    n_tiles = -(-max(h, w) // side) + 1 if span else 1
    boxes = []
    for k in range(n_tiles):
        off = round(span * k / (n_tiles - 1)) if n_tiles > 1 else 0
        tile = frame[:, off : off + side] if w >= h else frame[off : off + side, :]
        for x1, y1, x2, y2, s in _detect_square(net, tile):
            if w >= h:
                boxes.append(((off + x1 * side) / w, y1, (off + x2 * side) / w, y2, s))
            else:
                boxes.append((x1, (off + y1 * side) / h, x2, (off + y2 * side) / h, s))

    kept: list[tuple] = []
    for b in sorted(boxes, key=lambda b: -b[4]):
        if all(_iou(b, k) < 0.3 for k in kept):
            kept.append(b)
    return [
        {"cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2, "w": x2 - x1, "h": y2 - y1, "score": s}
        for x1, y1, x2, y2, s in kept
        if x2 - x1 >= MIN_FACE_WIDTH
    ]


def sample_faces(source: Path, start: float, end: float, net) -> tuple[list[tuple[float, list[dict]]], int, int]:
    """Detections at ~SAMPLE_FPS between start and end (clip-relative times)."""
    import cv2

    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open {source}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    step = max(1, round(fps / SAMPLE_FPS))
    first = int(start * fps)
    last = int(end * fps)
    cap.set(cv2.CAP_PROP_POS_FRAMES, first)
    samples = []
    idx = first
    while idx <= last:
        ok, frame = cap.read()
        if not ok:
            break
        samples.append((idx / fps - start, detect_faces(net, frame)))
        for _ in range(step - 1):  # grab() skips decoding-to-BGR for frames we don't analyse
            if not cap.grab():
                break
        idx += step
    cap.release()
    return samples, width, height


def choose_subject(samples: list[tuple[float, list[dict]]]) -> list[dict | None]:
    """One face (or None) per sample, following one person with hysteresis."""
    chosen: list[dict | None] = []
    current: dict | None = None
    challenger_since: float | None = None
    for t, faces in samples:
        if not faces:
            chosen.append(None)
            continue
        biggest = max(faces, key=lambda f: f["w"] * f["h"])
        if current is None:
            current = biggest
            chosen.append(current)
            continue
        nearest = min(faces, key=lambda f: abs(f["cx"] - current["cx"]) + abs(f["cy"] - current["cy"]))
        if biggest is not nearest and biggest["w"] * biggest["h"] > SWITCH_AREA_RATIO * nearest["w"] * nearest["h"]:
            challenger_since = t if challenger_since is None else challenger_since
            if t - challenger_since >= SWITCH_HOLD_S:
                nearest, challenger_since = biggest, None
        else:
            challenger_since = None
        current = nearest
        chosen.append(current)
    return chosen


def camera_path(times: list[float], targets: list[float | None], crop_frac: float) -> list[tuple[float, bool]]:
    """Smooth subject x-centres into a camera path: (x-centre 0-1 of source width, is_cut) per sample."""
    # Fill gaps by holding the last seen position (or the first one, at the start).
    first_seen = next((x for x in targets if x is not None), 0.5)
    filled, last = [], first_seen
    for x in targets:
        last = x if x is not None else last
        filled.append(last)

    half = MEDIAN_WINDOW // 2
    smoothed = [statistics.median(filled[max(0, i - half) : i + half + 1]) for i in range(len(filled))]

    lo, hi = crop_frac / 2, 1 - crop_frac / 2  # keep the crop window inside the frame
    clamp = lambda v: min(max(v, lo), hi)  # noqa: E731
    cam = clamp(smoothed[0])
    path = [(cam, False)]
    following = False
    for i in range(1, len(smoothed)):
        dt = times[i] - times[i - 1]
        target = clamp(smoothed[i])
        delta = target - cam
        if abs(delta) > crop_frac / 2:
            # Subject is outside the window (usually a switch to another speaker): cut, don't whip-pan.
            cam, following = target, False
            path.append((cam, True))
            continue
        else:
            # Ignore drift inside the dead zone; once it's exceeded, re-centre fully at a capped speed.
            following = following or abs(delta) > DEAD_ZONE
            if following:
                max_move = MAX_PAN_PER_S * dt
                cam += max(-max_move, min(max_move, delta))
                following = abs(target - cam) > 0.005
        path.append((cam, False))
    return path


def track_clip(source: Path, start: float, end: float, net) -> dict:
    samples, width, height = sample_faces(source, start, end, net)
    crop_frac = min(1.0, (height * 9 / 16) / width) if width else 1.0
    chosen = choose_subject(samples)
    coverage = sum(1 for c in chosen if c is not None) / max(1, len(chosen))
    times = [t for t, _ in samples]
    path = camera_path(times, [c["cx"] if c else None for c in chosen], crop_frac)
    faces_per_sample = [len(f) for _, f in samples]
    use = coverage >= MIN_COVERAGE and crop_frac < 0.98
    return {
        "start": start,
        "end": end,
        "frame_width": width,
        "frame_height": height,
        "crop_fraction": round(crop_frac, 4),
        "coverage": round(coverage, 3),
        "max_faces": max(faces_per_sample, default=0),
        "mode": "track" if use else "none",
        "reason": (
            "face tracked"
            if use
            else ("source is already vertical" if crop_frac >= 0.98 else f"faces in only {coverage:.0%} of samples")
        ),
        # cut=true: jump to this position at t instead of easing from the previous keyframe.
        "keyframes": [{"t": round(t, 3), "cx": round(x, 4), **({"cut": True} if cut else {})} for t, (x, cut) in zip(times, path)],
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", default=None, help="Comma-separated ranks to track (default: all)")
    p.add_argument("--force", action="store_true", help="Re-track even when cached tracks match")
    args = p.parse_args()

    try:
        import cv2
    except ImportError:
        sys.exit("opencv-python-headless is not installed. Run: pip install -r requirements.txt")

    if not HIGHLIGHTS_JSON.exists():
        sys.exit("Run 03_detect_viral.py first (output/highlights.json is missing).")
    highlights = read_json(HIGHLIGHTS_JSON)
    only = {int(x) for x in args.only.split(",")} if args.only else None

    source_mtime = int(SOURCE_VIDEO.stat().st_mtime)
    cache = read_json(FACE_TRACKS_JSON) if FACE_TRACKS_JSON.exists() else {}
    if cache.get("version") != TRACK_VERSION or cache.get("source_mtime") != source_mtime:
        cache = {"version": TRACK_VERSION, "source_mtime": source_mtime, "clips": {}}

    prototxt, weights = ensure_model()
    net = cv2.dnn.readNetFromCaffe(str(prototxt), str(weights))

    for clip in highlights["clips"]:
        rank = clip["rank"]
        if only and rank not in only:
            continue
        key = str(rank)
        prev = cache["clips"].get(key)
        if prev and not args.force and prev["start"] == clip["start"] and prev["end"] == clip["end"]:
            log(f"#{rank}: face track up to date ({prev['mode']}).")
            continue
        log(f"#{rank}: tracking faces in {clip['start']:.2f}s → {clip['end']:.2f}s…")
        result = track_clip(SOURCE_VIDEO, clip["start"], clip["end"], net)
        cache["clips"][key] = result
        log(f"#{rank}: {result['mode']} — {result['reason']} (coverage {result['coverage']:.0%}, up to {result['max_faces']} face(s)).")
        write_json(FACE_TRACKS_JSON, cache)

    write_json(FACE_TRACKS_JSON, cache)
    log(f"Wrote {FACE_TRACKS_JSON.relative_to(ROOT)}.")


if __name__ == "__main__":
    main()
