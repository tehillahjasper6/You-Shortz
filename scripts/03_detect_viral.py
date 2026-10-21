#!/usr/bin/env python3
"""Phase 2 — Pick the most interesting sections.

Sends the word-timed transcript to an LLM that picks the most interesting,
intriguing sections of the video (no scoring rubric), ranked from most to
least gripping, with absolute timestamps. The response is validated against
the contract (0 <= start < end <= duration, 15-60 s, no overlaps, hook <= 10
words); a violating response is retried once with a stricter instruction.

Accepted boundaries are then snapped so no clip starts or ends mid-word:
start lands 0.2-0.4 s before the first word, end 0.2-0.4 s after the last,
preferring silence gaps found with FFmpeg's silencedetect.

Writes output/highlights.json — edit it freely, then re-render.

Providers:
  anthropic  Claude (default model claude-sonnet-5-5; needs ANTHROPIC_API_KEY)
  gemini     Gemini (default model gemini-2.5-flash; needs GEMINI_API_KEY)
  session    The Claude running this Claude Code session picks them: the
             script writes output/selection_request.md and exits with code 3;
             Claude writes output/selection_response.json and re-runs. No key.
  heuristic  Offline keyword/pace picker — no API key, for dry runs
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from common import (
    AUDIO_WAV,
    HIGHLIGHTS_JSON,
    INGEST_JSON,
    TRANSCRIPT_JSON,
    WORK_DIR,
    await_session,
    ffmpeg_cmd,
    log,
    read_json,
    rel,
    request_id,
    session_response,
    run,
    write_json,
)

MIN_CLIP_S = 15.0
MAX_CLIP_S = 60.0
PAD_MIN_S = 0.2
PAD_MAX_S = 0.4
PAD_TARGET_S = 0.3
PHRASE_SNAP_BEFORE_S = 1.5
PHRASE_SNAP_AFTER_S = 1.0
MAX_HOOK_WORDS = 10
MAX_YT_TITLE = 100
FPS = 30

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5-5",
    "gemini": "gemini-2.5-flash",
    "session": "claude-code-session",
    "heuristic": "heuristic-v1",
}
SESSION_NAME = "selection"
RETRY_STATE = WORK_DIR / "selection_retry.json"

CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number", "description": "Absolute start time in seconds from the start of the source video"},
                    "end": {"type": "number", "description": "Absolute end time in seconds from the start of the source video"},
                    "hook_text": {"type": "string", "description": "On-screen hook, at most 10 words"},
                    "hook_variants": {
                        "type": "array",
                        "description": "Exactly three alternative hooks for A/B testing",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "predicted_ctr": {"type": "number", "description": "Predicted click-through rate, 0-1"},
                            },
                            "required": ["text", "predicted_ctr"],
                            "additionalProperties": False,
                        },
                    },
                    "reasoning": {"type": "string", "description": "Why this section is interesting"},
                    "tiktok_description": {"type": "string"},
                    "instagram_caption": {"type": "string"},
                    "youtube_title": {"type": "string"},
                },
                "required": [
                    "start", "end", "hook_text", "hook_variants", "reasoning",
                    "tiktok_description", "instagram_caption", "youtube_title",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a short-form video editor cutting a long video into vertical clips for TikTok, Instagram Reels and YouTube Shorts.

Pick the most interesting and intriguing sections of the video: the parts a viewer would least want to scroll past. Look for a surprising claim, a question that demands an answer, a story with a payoff, a strong opinion, a revealing moment, or a striking insight. Each clip should open on something that makes people want to keep watching, and end once that curiosity has been paid off.

Rules for each clip:
- start and end are ABSOLUTE times in seconds from the beginning of the source video, read directly from the transcript timestamps. Never use offsets relative to a segment or to another clip.
- Duration (end - start) must be between 15 and 60 seconds inclusive.
- Start 0.2-0.4 s before the first word of the hook; end 0.2-0.4 s after the last word of the payoff.
- Never cut in the middle of a word or a phrase. A clip must stand alone: no dangling "and so..." openings, no unresolved setups.
- Clips must not overlap each other.
- hook_text is the on-screen hook shown in the first seconds: at most 10 words, punchy, no hashtags. It may paraphrase.
- hook_variants: three alternative hooks (each at most 10 words) with a predicted click-through rate between 0 and 1.
- reasoning: one or two sentences on what makes this section interesting.
- tiktok_description: conversational, 1-2 sentences plus 3-5 hashtags.
- instagram_caption: a slightly longer caption with a call to action and 5-8 hashtags.
- youtube_title: at most 100 characters, no hashtags required.

Order the clips from most to least interesting."""


# --------------------------------------------------------------------------- prompt


def format_transcript(transcript: dict, word_times: bool) -> str:
    lines = []
    for seg in transcript["segments"]:
        if word_times:
            body = " ".join(f"{w['word']}<{w['start']:.2f}>" for w in seg["words"])
        else:
            body = seg["text"]
        lines.append(f"[{seg['start']:.2f}-{seg['end']:.2f}] {body}")
    return "\n".join(lines)


def build_user_prompt(transcript: dict, n: int, duration: float, word_times: bool, errors: list[str] | None) -> str:
    timing_note = (
        "Each line is one segment: [segment_start-segment_end] followed by words, each tagged with its absolute start time as word<seconds>."
        if word_times
        else "Each line is one segment: [segment_start-segment_end] followed by its text."
    )
    parts = [
        f"Source video duration: {duration:.2f} seconds.",
        f"Return exactly {n} clips.",
        timing_note,
        "",
        "<transcript>",
        format_transcript(transcript, word_times),
        "</transcript>",
    ]
    if errors:
        parts += [
            "",
            "Your previous answer violated the output contract:",
            *[f"- {e}" for e in errors],
            "",
            f"Fix every violation. Every start must be >= 0, every end must be <= {duration:.2f}, "
            f"every end must be greater than its start, and every duration must be between {MIN_CLIP_S:.0f} and {MAX_CLIP_S:.0f} seconds. "
            "Timestamps are absolute seconds copied from the transcript, not relative offsets.",
        ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- providers


def call_anthropic(model: str, effort: str, system: str, user: str) -> dict:
    try:
        import anthropic
    except ImportError:
        sys.exit("The anthropic package is not installed. Run: pip install -r requirements.txt")

    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": CLIP_SCHEMA}},
            # On a safety decline, let the API re-run the request on its recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError:
        sys.exit("Anthropic authentication failed. Set ANTHROPIC_API_KEY (or run `ant auth login`).")
    except anthropic.RateLimitError as e:
        sys.exit(f"Anthropic rate limit hit; try again shortly. ({e})")
    except anthropic.APIStatusError as e:
        sys.exit(f"Anthropic API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError as e:
        sys.exit(f"Could not reach the Anthropic API: {e}")

    if response.stop_reason == "refusal":
        raise ValueError("The model declined to pick from this transcript.")
    if response.stop_reason == "max_tokens":
        raise ValueError("Response was cut off at max_tokens.")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def call_gemini(model: str, system: str, user: str) -> dict:
    try:
        from google import genai
    except ImportError:
        sys.exit("The google-genai package is not installed. Run: pip install google-genai")

    client = genai.Client()
    response = client.models.generate_content(
        model=model,
        contents=user,
        config={
            "system_instruction": system,
            "response_mime_type": "application/json",
            "response_json_schema": CLIP_SCHEMA,
        },
    )
    return json.loads(response.text)


HOOK_WORDS = re.compile(r"\b(why|how|what|secret|never|stop|nobody|everyone|mistake|truth|actually|wrong|crazy|insane|you)\b", re.I)
EMOTION_WORDS = re.compile(
    r"\b(love|hate|amazing|insane|crazy|shocked|shocking|unbelievable|hilarious|laugh|scared|angry|wow|incredible|terrible|beautiful|cried)\b",
    re.I,
)
SHARE_WORDS = re.compile(r"\b(you|your|everyone|nobody|people|friend|always|never|imagine)\b", re.I)
PRACTICAL_WORDS = re.compile(r"\b(how to|step|tip|trick|should|need to|way to|use|try|first|second|rule|because|means)\b", re.I)


def heuristic_candidates(transcript: dict, n: int, duration: float) -> dict:
    """Keyword/pace picker for offline runs. Same output shape as the LLM, ordered best first."""
    segs = transcript["segments"]
    # Aim for ~32 s, but shorter when the video is too short to fit n of those side by side.
    target = min(32.0, max(MIN_CLIP_S + 3, 0.8 * duration / max(n, 1)))

    def scale(hits: int, words: int, k: float) -> int:
        return max(1, min(10, round(1 + 9 * min(1.0, k * hits / max(words, 1)))))

    windows = []
    for i in range(len(segs)):
        for j in range(i, len(segs)):
            start, end = segs[i]["start"] - PAD_TARGET_S, segs[j]["end"] + PAD_TARGET_S
            length = end - start
            if length > MAX_CLIP_S:
                break
            if length < MIN_CLIP_S:
                continue
            text = " ".join(s["text"] for s in segs[i : j + 1])
            words = len(text.split())
            first = segs[i]["text"]
            # Rough "interesting" signals: an intriguing opener, emotional and
            # second-person language, concrete how-to talk, and a brisk pace.
            signals = (
                min(10, scale(len(HOOK_WORDS.findall(first)), len(first.split()), 4) + (2 if "?" in first else 0))
                + min(10, scale(len(EMOTION_WORDS.findall(text)) + text.count("!"), words, 12))
                + scale(len(SHARE_WORDS.findall(text)), words, 8)
                + scale(len(PRACTICAL_WORDS.findall(text)), words, 10)
            )
            pace = words / length  # brisk delivery (~2.5-3.5 wps) holds attention
            interest = signals + (1.5 if 2.3 <= pace <= 3.8 else 0) - abs(length - target) / 4
            windows.append((interest, max(0.0, start), min(duration, end), first, text))

    windows.sort(key=lambda w: -w[0])
    chosen = []
    for w in windows:
        if all(w[2] <= c[1] or w[1] >= c[2] for c in chosen):
            chosen.append(w)
        if len(chosen) == n:
            break

    clips = []
    for _, start, end, first, text in chosen:
        hook = " ".join(re.sub(r"[^\w'?!\- ]", "", first).split()[:MAX_HOOK_WORDS])
        topic = " ".join(text.split()[:6])
        clips.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "hook_text": hook,
                "hook_variants": [
                    {"text": hook, "predicted_ctr": 0.05},
                    {"text": " ".join(f"Wait for it: {hook}".split()[:MAX_HOOK_WORDS]), "predicted_ctr": 0.045},
                    {"text": " ".join(f"Nobody talks about this: {topic}".split()[:MAX_HOOK_WORDS]), "predicted_ctr": 0.04},
                ],
                "reasoning": "Offline heuristic pick: intriguing opener, emotional and second-person language, and a brisk pace.",
                "tiktok_description": f"{hook} 👀 #fyp #viral #learnontiktok",
                "instagram_caption": f"{hook}\n\nSave this for later and share it with someone who needs it. #reels #viral #explore #tips #mindset",
                "youtube_title": hook[:MAX_YT_TITLE] or "You need to hear this",
            }
        )
    return {"clips": clips}


# --------------------------------------------------------------------------- validation


def validate(payload: dict, duration: float, n: int) -> list[str]:
    errors: list[str] = []
    clips = payload.get("clips")
    if not isinstance(clips, list) or not clips:
        return ["'clips' must be a non-empty array"]
    if len(clips) < n:
        errors.append(f"expected {n} clips, got {len(clips)}")
    for i, c in enumerate(clips, 1):
        s, e = c.get("start"), c.get("end")
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
            errors.append(f"clip {i}: start/end must be numbers")
            continue
        if s < 0:
            errors.append(f"clip {i}: start {s} is negative")
        if e > duration:
            errors.append(f"clip {i}: end {e} is past the video duration {duration:.2f}")
        if e <= s:
            errors.append(f"clip {i}: end {e} is not after start {s}")
        elif not (MIN_CLIP_S <= e - s <= MAX_CLIP_S):
            errors.append(f"clip {i}: duration {e - s:.2f}s is outside {MIN_CLIP_S:.0f}-{MAX_CLIP_S:.0f}s")
        if len(str(c.get("hook_text", "")).split()) > MAX_HOOK_WORDS:
            errors.append(f"clip {i}: hook_text has more than {MAX_HOOK_WORDS} words")
        if not str(c.get("hook_text", "")).strip():
            errors.append(f"clip {i}: hook_text is empty")
        if len(str(c.get("youtube_title", ""))) > MAX_YT_TITLE:
            errors.append(f"clip {i}: youtube_title exceeds {MAX_YT_TITLE} characters")
    for i, a in enumerate(clips, 1):
        for j, b in enumerate(clips[i:], i + 1):
            try:
                overlap = min(a["end"], b["end"]) - max(a["start"], b["start"])
            except (KeyError, TypeError):
                continue
            if overlap > 0:
                errors.append(f"clips {i} and {j} overlap by {overlap:.2f}s")
    return errors


def clip_is_valid(c: dict, duration: float) -> bool:
    return not validate({"clips": [c]}, duration, 1)


# --------------------------------------------------------------------------- boundary snapping


def detect_silences(audio: Path, noise_db: float = -35.0, min_silence: float = 0.15) -> list[tuple[float, float]]:
    cache = WORK_DIR / "silences.json"
    key = {"audio_mtime": int(audio.stat().st_mtime), "noise_db": noise_db, "min_silence": min_silence}
    if cache.exists():
        data = read_json(cache)
        if data.get("key") == key:
            return [tuple(x) for x in data["silences"]]
    log("Detecting silence gaps for boundary snapping…")
    proc = run(ffmpeg_cmd() + ["-hide_banner", "-nostats", "-i", str(audio), "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", proc.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", proc.stderr)]
    silences = [(max(0.0, s), e) for s, e in zip(starts, ends)]
    write_json(cache, {"key": key, "silences": silences})
    return silences


def _closest_in(target: float, lo: float, hi: float, silences: list[tuple[float, float]]) -> float:
    """Closest point to target within [lo, hi], preferring points inside a silence gap."""
    best = None
    for s, e in silences:
        a, b = max(lo, s), min(hi, e)
        if a <= b:
            p = min(max(target, a), b)
            if best is None or abs(p - target) < abs(best - target):
                best = p
    return best if best is not None else min(max(target, lo), hi)


def snap_start(t: float, words: list[dict], seg_starts: list[float], silences) -> tuple[float, int]:
    # First word that is not already over by t; cutting inside it would clip the word.
    i = next((k for k, w in enumerate(words) if w["end"] > t), len(words) - 1)
    # Prefer a nearby phrase start so the clip opens on a full sentence.
    anchor = words[i]["start"]
    near = [s for s in seg_starts if anchor - PHRASE_SNAP_BEFORE_S <= s <= anchor + PHRASE_SNAP_AFTER_S]
    if near:
        anchor = min(near, key=lambda s: abs(s - anchor))
        i = next(k for k, w in enumerate(words) if w["start"] >= anchor - 1e-6)
    prev_end = words[i - 1]["end"] if i > 0 else 0.0
    lo, hi = max(prev_end, anchor - PAD_MAX_S, 0.0), anchor - PAD_MIN_S
    if lo > hi:  # words are tighter than the pad window; cut in the gap
        lo, hi = max(prev_end, 0.0), anchor
    return _closest_in(anchor - PAD_TARGET_S, lo, hi, silences), i


def snap_end(t: float, words: list[dict], seg_ends: list[float], duration: float, silences) -> tuple[float, int]:
    j = max((k for k, w in enumerate(words) if w["start"] < t), default=0)
    anchor = words[j]["end"]
    near = [e for e in seg_ends if anchor - PHRASE_SNAP_AFTER_S <= e <= anchor + PHRASE_SNAP_BEFORE_S]
    if near:
        anchor = min(near, key=lambda e: abs(e - anchor))
        j = max(k for k, w in enumerate(words) if w["end"] <= anchor + 1e-6)
    next_start = words[j + 1]["start"] if j + 1 < len(words) else duration
    lo, hi = anchor + PAD_MIN_S, min(next_start, anchor + PAD_MAX_S, duration)
    if lo > hi:
        lo, hi = anchor, min(next_start, duration)
    return _closest_in(anchor + PAD_TARGET_S, lo, hi, silences), j


def snap_clip(c: dict, words: list[dict], segments: list[dict], duration: float, silences) -> dict:
    seg_starts = [s["start"] for s in segments]
    seg_ends = [s["end"] for s in segments]
    start, i = snap_start(c["start"], words, seg_starts, silences)
    end, j = snap_end(c["end"], words, seg_ends, duration, silences)
    # Snapping can push a clip just outside 15-60 s; walk the end word by word to fix it.
    while end - start > MAX_CLIP_S and j > i:
        j -= 1
        end, j = snap_end(words[j]["end"], words, [], duration, silences)
    while end - start < MIN_CLIP_S and j + 1 < len(words):
        j += 1
        end, j = snap_end(words[j]["end"], words, [], duration, silences)
    return {**c, "llm_start": c["start"], "llm_end": c["end"], "start": round(start, 3), "end": round(end, 3)}


# --------------------------------------------------------------------------- main


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--clips", type=int, default=3, help="How many clips to keep (LLM is asked for at least 3)")
    p.add_argument("--provider", choices=list(DEFAULT_MODELS), default="anthropic")
    p.add_argument("--model", default=None, help="Override the provider's default model")
    p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"], help="Claude effort level")
    p.add_argument("--no-word-times", action="store_true", help="Send segment-level timestamps only (cheaper for very long videos)")
    p.add_argument("--no-snap", action="store_true", help="Keep the model's boundaries as returned")
    p.add_argument("--force", action="store_true", help="Overwrite an existing highlights.json (your edits are lost)")
    args = p.parse_args()

    if HIGHLIGHTS_JSON.exists() and not args.force:
        log("highlights.json already exists; skipping (edit it and re-render, or use --force to re-detect).")
        return
    if not TRANSCRIPT_JSON.exists():
        sys.exit("Run 02_transcribe.py first (output/transcript.json is missing).")

    transcript = read_json(TRANSCRIPT_JSON)
    ingest = read_json(INGEST_JSON) if INGEST_JSON.exists() else {}
    duration = float(ingest.get("duration") or transcript["duration"])
    if duration < MIN_CLIP_S:
        sys.exit(f"Video is only {duration:.1f}s; clips must be at least {MIN_CLIP_S:.0f}s.")

    want = max(1, args.clips)
    ask = min(max(want, 3), 10)
    model = args.model or DEFAULT_MODELS[args.provider]
    log(f"Picking the most interesting sections with {args.provider} ({model}), asking for {ask}…")

    if args.provider == "session":
        payload, errors = session_selection(transcript, ask, duration, not args.no_word_times)
    else:
        payload, errors = api_selection(args, model, transcript, ask, duration)

    clips = [c for c in payload.get("clips", []) if clip_is_valid(c, duration)]
    if errors:
        log(f"Retry still had {len(errors)} violation(s); keeping the {len(clips)} clip(s) that pass on their own.")
    if not clips:
        sys.exit("No valid clips were returned. Try another --model, or --provider heuristic for an offline pass.")
    finish(args, model, transcript, ingest, duration, want, clips)


def session_selection(transcript: dict, ask: int, duration: float, word_times: bool) -> tuple[dict, list[str]]:
    """Pick via the Claude Code session, with the same one stricter retry as the API path."""
    prompt = SYSTEM_PROMPT + "\n\n" + build_user_prompt(transcript, ask, duration, word_times, None)
    first = request_id(prompt)
    answer = session_response(SESSION_NAME, first)
    if answer is not None:
        errors = validate(answer, duration, ask)
        if not errors:
            return answer, []
        write_json(RETRY_STATE, {"first": first, "errors": errors})
        for e in errors:
            log(f"  - {e}")
        retry = SYSTEM_PROMPT + "\n\n" + build_user_prompt(transcript, ask, duration, word_times, errors)
        await_session(SESSION_NAME, retry, CLIP_SCHEMA, note="This is the one retry: the previous answer broke the contract (see the end of the task).")

    state = read_json(RETRY_STATE) if RETRY_STATE.exists() else {}
    if state.get("first") == first:
        retry = SYSTEM_PROMPT + "\n\n" + build_user_prompt(transcript, ask, duration, word_times, state["errors"])
        answer = session_response(SESSION_NAME, request_id(retry))
        if answer is not None:
            return answer, validate(answer, duration, ask)
        await_session(SESSION_NAME, retry, CLIP_SCHEMA, note="This is the one retry: the previous answer broke the contract (see the end of the task).")
    await_session(SESSION_NAME, prompt, CLIP_SCHEMA)
    raise AssertionError("unreachable")


def api_selection(args, model: str, transcript: dict, ask: int, duration: float) -> tuple[dict, list[str]]:
    def request(errors: list[str] | None) -> dict:
        if args.provider == "heuristic":
            return heuristic_candidates(transcript, ask, duration)
        user = build_user_prompt(transcript, ask, duration, not args.no_word_times, errors)
        if args.provider == "anthropic":
            return call_anthropic(model, args.effort, SYSTEM_PROMPT, user)
        return call_gemini(model, SYSTEM_PROMPT, user)

    def attempt(errors: list[str] | None) -> tuple[dict, list[str]]:
        try:
            payload = request(errors)
        except (ValueError, json.JSONDecodeError) as e:
            return {"clips": []}, [f"response was not usable JSON: {e}"]
        return payload, validate(payload, duration, ask)

    payload, errors = attempt(None)
    if errors and args.provider != "heuristic":  # the heuristic is deterministic; a retry can't change it
        log("Response violated the contract; retrying once with a stricter instruction:")
        for e in errors:
            log(f"  - {e}")
        payload, errors = attempt(errors)
    return payload, errors


def finish(args, model: str, transcript: dict, ingest: dict, duration: float, want: int, clips: list[dict]) -> None:
    words = [w for s in transcript["segments"] for w in s["words"]]
    if not args.no_snap and words:
        silences = detect_silences(AUDIO_WAV) if AUDIO_WAV.exists() else []
        clips = [snap_clip(c, words, transcript["segments"], duration, silences) for c in clips]
        clips = [c for c in clips if clip_is_valid(c, duration)]

    # Clips arrive ordered most interesting first; drop overlaps introduced by
    # snapping, keeping the earlier (more interesting) clip.
    kept: list[dict] = []
    for c in clips:
        if all(c["end"] <= k["start"] or c["start"] >= k["end"] for k in kept):
            kept.append(c)
    kept = kept[:want]

    out_clips = []
    for rank, c in enumerate(kept, 1):
        out_clips.append(
            {
                "rank": rank,
                "start": c["start"],
                "end": c["end"],
                "duration": round(c["end"] - c["start"], 3),
                "hook_text": c["hook_text"].strip(),
                "reasoning": c["reasoning"],
                "hook_variants": sorted(c.get("hook_variants", []), key=lambda v: -v.get("predicted_ctr", 0)),
                "platform": {
                    "tiktok_description": c["tiktok_description"],
                    "instagram_caption": c["instagram_caption"],
                    "youtube_title": c["youtube_title"][:MAX_YT_TITLE],
                },
                **({"llm_start": c["llm_start"], "llm_end": c["llm_end"]} if "llm_start" in c else {}),
            }
        )

    write_json(
        HIGHLIGHTS_JSON,
        {
            "version": 1,
            "source": "public/source.mp4",
            "title": ingest.get("title"),
            "video_duration": duration,
            "fps": FPS,
            "provider": args.provider,
            "model": model,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "_edit_me": "Adjust start/end (absolute seconds, 15-60s long), hook_text or platform copy, then run `npm run render`.",
            "clips": out_clips,
        },
    )
    log(f"Wrote {rel(HIGHLIGHTS_JSON)}:")
    for c in out_clips:
        log(f"  #{c['rank']} [{c['start']:.2f} → {c['end']:.2f}] {c['hook_text']}")


if __name__ == "__main__":
    main()
