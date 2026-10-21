#!/usr/bin/env python3
"""Phase 1b — Word-level transcription.

Produces output/transcript.json:

  {text, language, duration, source,
   segments: [{text, start, end, words: [{word, start, end}]}]}

Uses the YouTube subtitles fetched during ingestion when they exist (auto
captions carry per-word timing; manual captions get word times interpolated
across each cue). Otherwise — or with --whisper — transcribes output/audio.wav
with faster-whisper (word_timestamps=True).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from common import AUDIO_WAV, INGEST_JSON, TRANSCRIPT_JSON, WORKSPACE, log, media_duration, read_json, rel, write_json

# Non-speech cue tokens such as [Music] or (applause) never belong in captions.
NON_SPEECH = re.compile(r"^[\[\(][^\]\)]*[\]\)]$")
SENTENCE_END = re.compile(r"[.!?…]['\")\]]*$")
MAX_SEGMENT_WORDS = 30
# Auto-captions carry no punctuation, so pauses are the main sentence signal.
SEGMENT_GAP_S = 0.5


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def words_from_json3(path: Path) -> list[dict]:
    """Flatten a YouTube json3 subtitle file into timed words."""
    data = read_json(path)
    events = [e for e in data.get("events", []) if e.get("segs") and "tStartMs" in e]
    words: list[dict] = []
    for i, ev in enumerate(events):
        ev_start = ev["tStartMs"] / 1000
        ev_end = ev_start + ev.get("dDurationMs", 0) / 1000
        if i + 1 < len(events):
            # Rolling auto-captions overlap; speech in this cue ends when the next starts.
            next_start = events[i + 1]["tStartMs"] / 1000
            ev_end = min(ev_end, next_start) if ev_end > ev_start else next_start
        segs = [s for s in ev["segs"] if _clean(s.get("utf8", ""))]
        if not segs:
            continue
        if len(segs) > 1:
            # Auto captions: one seg per word with an offset into the event.
            for j, s in enumerate(segs):
                start = ev_start + s.get("tOffsetMs", 0) / 1000
                nxt = ev_start + segs[j + 1].get("tOffsetMs", 0) / 1000 if j + 1 < len(segs) else ev_end
                for token in _clean(s["utf8"]).split(" "):
                    words.append({"word": token, "start": start, "end": max(nxt, start + 0.05)})
        else:
            # Manual captions: one cue of text; spread the words across it by length.
            tokens = _clean(segs[0]["utf8"]).split(" ")
            total = sum(len(t) + 1 for t in tokens)
            t = ev_start
            span = max(ev_end - ev_start, 0.05 * len(tokens))
            for token in tokens:
                dur = span * (len(token) + 1) / total
                words.append({"word": token, "start": t, "end": t + dur})
                t += dur

    words = [w for w in words if not NON_SPEECH.match(w["word"])]
    words.sort(key=lambda w: w["start"])
    # Collapse repeated words that rolling auto-captions sometimes emit, and stop
    # each word where the next one starts.
    deduped: list[dict] = []
    for w in words:
        if deduped and w["word"] == deduped[-1]["word"] and abs(w["start"] - deduped[-1]["start"]) < 0.05:
            continue
        deduped.append(w)
    for a, b in zip(deduped, deduped[1:]):
        a["end"] = min(a["end"], b["start"]) if b["start"] > a["start"] else a["end"]
    return deduped


def segments_from_words(words: list[dict]) -> list[dict]:
    """Group a flat word list into sentence-like segments."""
    segments: list[dict] = []
    current: list[dict] = []
    for i, w in enumerate(words):
        current.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["start"] - w["end"]) if nxt else 0
        if nxt is None or SENTENCE_END.search(w["word"]) or gap > SEGMENT_GAP_S or len(current) >= MAX_SEGMENT_WORDS:
            segments.append(
                {"text": " ".join(x["word"] for x in current), "start": current[0]["start"], "end": current[-1]["end"], "words": current}
            )
            current = []
    return segments


def transcribe_whisper(audio: Path, model_size: str, language: str | None, device: str) -> tuple[list[dict], str]:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("faster-whisper is not installed. Run: pip install -r requirements.txt")

    if device == "auto":
        try:
            import ctranslate2

            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    log(f"Loading faster-whisper '{model_size}' on {device} ({compute_type})…")
    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    seg_iter, info = model.transcribe(
        str(audio), language=language, word_timestamps=True, vad_filter=True, beam_size=5
    )
    segments = []
    for seg in seg_iter:
        words = [
            {"word": w.word.strip(), "start": w.start, "end": w.end}
            for w in (seg.words or [])
            if w.word.strip()
        ]
        if not words:
            continue
        segments.append({"text": seg.text.strip(), "start": seg.start, "end": seg.end, "words": words})
        log(f"  [{seg.start:7.2f} → {seg.end:7.2f}] {seg.text.strip()}")
    return segments, info.language


def finalize(segments: list[dict], language: str, duration: float, source: str) -> dict:
    def r(x: float) -> float:
        return round(float(x), 3)

    out_segments = []
    for seg in segments:
        words = [{"word": w["word"], "start": r(w["start"]), "end": r(max(w["end"], w["start"]))} for w in seg["words"]]
        out_segments.append({"text": _clean(seg["text"]), "start": r(words[0]["start"]), "end": r(words[-1]["end"]), "words": words})
    return {
        "text": " ".join(s["text"] for s in out_segments),
        "language": language,
        "duration": r(duration),
        "source": source,
        "segments": out_segments,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--whisper", action="store_true", help="Ignore YouTube subtitles and always run faster-whisper")
    p.add_argument("--model", default="small", help="faster-whisper model size (tiny/base/small/medium/large-v3)")
    p.add_argument("--language", default=None, help="Force a language code (default: auto-detect)")
    p.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    p.add_argument("--force", action="store_true", help="Overwrite an existing transcript.json")
    args = p.parse_args()

    if TRANSCRIPT_JSON.exists() and not args.force:
        log("transcript.json already exists; skipping (use --force to redo).")
        return
    if not INGEST_JSON.exists():
        sys.exit("Run 01_ingest.py first (output/ingest.json is missing).")

    ingest = read_json(INGEST_JSON)
    duration = float(ingest.get("duration") or media_duration(AUDIO_WAV))
    subs = ingest.get("subtitles")

    if subs and not args.whisper:
        path = WORKSPACE / subs["path"]
        log(f"Using YouTube {subs['kind']} subtitles ({subs['lang']}) for word timing.")
        words = words_from_json3(path)
        if not words:
            sys.exit("Subtitle file contained no words; re-run with --whisper.")
        transcript = finalize(segments_from_words(words), subs["lang"].split("-")[0], duration, f"youtube_{subs['kind']}_subtitles")
    else:
        if not AUDIO_WAV.exists():
            sys.exit("output/audio.wav is missing; re-run 01_ingest.py.")
        segments, language = transcribe_whisper(AUDIO_WAV, args.model, args.language, args.device)
        if not segments:
            sys.exit("Whisper found no speech in the audio.")
        transcript = finalize(segments, language, duration, f"faster-whisper:{args.model}")

    write_json(TRANSCRIPT_JSON, transcript)
    n_words = sum(len(s["words"]) for s in transcript["segments"])
    log(f"Wrote {rel(TRANSCRIPT_JSON)} ({len(transcript['segments'])} segments, {n_words} words).")


if __name__ == "__main__":
    main()
