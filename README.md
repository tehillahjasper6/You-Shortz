# Viral Clipper

A local-first pipeline that turns a YouTube URL or a video file into ranked, ready-to-post **1080×1920** short clips for TikTok, Instagram Reels and YouTube Shorts. Each clip gets burned-in word-by-word captions, an animated hook, a progress bar and platform-specific copy.

```bash
npx tsx scripts/run.ts --url "https://youtube.com/watch?v=..." --clips 3
```

You get:

1. `output/highlights.json`: ranked clip candidates you can edit.
2. `output/clips/viral_{rank}_{hook_slug}.mp4`: H.264 / yuv420p / 30 fps / AAC 192 kbps.
3. `output/clips/viral_{rank}_{hook_slug}.metadata.json` per clip, plus a combined `output/clips/metadata.json`, holding the TikTok description, Reels caption and Shorts title.

`public/source.mp4` is never trimmed. Clips are cut non-destructively at render time, so editing `highlights.json` and re-running only re-renders. Nothing is downloaded or transcribed again.

---

## Setup

Requirements: Node 18+, Python 3.10+.

```bash
npm install                      # Remotion + its bundled FFmpeg/FFprobe
pip install -r requirements.txt  # yt-dlp, faster-whisper, anthropic
export ANTHROPIC_API_KEY=...     # or GEMINI_API_KEY with --provider gemini
```

You don't need a system FFmpeg. The Python scripts use Remotion's bundled binaries (`npx remotion ffmpeg`), then fall back to `$FFMPEG`/`$FFPROBE` or whatever is on `PATH`.

## The pipeline

Each phase is a separate script that writes an artifact you can review. A phase is skipped when its artifact already exists.

| Phase | Script | Output |
|---|---|---|
| 1a. Ingest | `scripts/01_ingest.py` | `public/source.mp4`, `output/audio.wav`, `output/ingest.json` |
| 1b. Transcribe | `scripts/02_transcribe.py` | `output/transcript.json` |
| 2. Detect | `scripts/03_detect_viral.py` | `output/highlights.json` |
| 3–5. Render | `scripts/04_render.ts` | `output/clips/*.mp4` + metadata |
| All | `scripts/run.ts` | runs the above in order |

### 1a. Ingest
- **YouTube:** `yt-dlp` fetches the best ≤1080p stream (H.264/MP4 preferred, so it can be stream-copied) and the manual or auto subtitles as `json3`.
- **Local files:** `.mp4`, `.mov` and `.mkv` are accepted. Non-H.264 or >1080p video is re-encoded to H.264.
- Audio is normalized to **−14 LUFS** (true peak −1.5 dBTP) with a two-pass `loudnorm`, then written as AAC 192k into `public/source.mp4`. A 16 kHz mono copy goes to `output/audio.wav` for transcription.
- Ingesting the same source again does nothing. A *different* source needs `--force`, and the old transcript and highlights are moved to `*.stale.json`.

### 1b. Transcribe
Writes `{text, segments: [{text, start, end, words: [{word, start, end}]}]}`. All times are absolute seconds in the source.
- If YouTube subtitles exist, they are used. Auto-captions carry per-word timing. Manual captions only time whole cues, so word times are spread across each cue by word length.
- Otherwise, or with `--whisper`, `faster-whisper` transcribes with `word_timestamps=True` and VAD. Use `--model tiny|base|small|medium|large-v3`; GPU is detected automatically.

### 2. Detect viral moments
The full transcript, with each word tagged by its absolute start time, goes to an LLM using a JSON-schema structured output. Each candidate gets four scores from 1 to 10: **Hook Strength**, **Emotional Resonance**, **Shareability** and **Practical Value**. `viral_score` is their mean × 10. Each candidate also returns a hook (≤10 words), three A/B hook variants with a predicted CTR, its reasoning, and platform copy.

- **Validation:** `start ≥ 0`, `end ≤ duration`, `end > start`, a length of 15–60 s, scores in range, no overlaps. A response that breaks the contract is retried **once** with the violations listed and a stricter instruction. After that, only the clips that pass on their own are kept.
- **Snap-to-cut:** boundaries move to the nearest phrase edge so a clip never opens or closes mid-word. Start lands 0.2–0.4 s before the first word and end 0.2–0.4 s after the last, preferring a point inside a silence gap found with FFmpeg's `silencedetect`. The model's original times are kept as `llm_start`/`llm_end`. Use `--no-snap` to skip this.
- **Providers:**
  - `--provider anthropic` (default): `claude-sonnet-5-5`, with server-side refusal fallback enabled.
  - `--provider gemini`: `gemini-2.5-flash`, needs `pip install google-genai`.
  - `--provider heuristic`: an offline keyword/pace scorer for dry runs, no key needed.

### 3–5. Render with Remotion
`remotion/ViralClip.tsx` is one composition, rendered once per highlight with `renderMedia()`:

- **Non-destructive trim:** `<Video>` from `@remotion/media` takes `trimBefore = round(start × 30)` and `durationInFrames = round((end − start) × 30)`. The source file is never pre-cut. If the browser can't decode the file, `@remotion/media` falls back to `<OffthreadVideo>` on its own.
- **Vertical reframe:** the source is fit into the 9:16 frame over a blurred, zoomed copy of itself.
- **Karaoke captions** (`components/Captions.tsx`): pages of 2–4 words built with `@remotion/captions`; the spoken word turns yellow with a small pop. Word times are made clip-relative by subtracting `trimBefore / fps`, not the raw `start`, so captions stay frame-aligned with the video. They sit above the bottom UI zone and clear of the right-hand action rail.
- **Hook overlay** (`components/HookOverlay.tsx`): `hook_text` punches in with `spring()` in the top third for the first 2.5 s.
- **Progress bar** (`components/ProgressBar.tsx`): along the top edge, since every platform draws its own scrubber at the bottom.
- **Loop-friendly ending:** audio fades over the last 0.5 s and the final frame is held for 4 frames, so the loop doesn't cut out abruptly.
- **Output:** H.264, `yuv420p` (BT.709, TV range), 30 fps, AAC 192 kbps.

## Editing and re-rendering

```bash
npx tsx scripts/run.ts --url "..." --clips 3 --no-render   # stop after highlights.json
$EDITOR output/highlights.json                             # change start/end/hook_text/platform copy
npm run render                                             # or: npx tsx scripts/run.ts
npm run render -- --only 2                                 # re-render a single clip
```

A hand edit outside 15–60 s triggers a warning but still renders. An edit that can't render (end before start, past the end of the video) is skipped with an error. `--redetect` throws away your edits and asks the model again.

Preview and tweak the composition interactively with `npm run studio`. Props are editable in the sidebar.

## Options (`scripts/run.ts --help`)

| Flag | Effect |
|---|---|
| `--url` / `--file` | Source to ingest. Omit both to re-render from existing artifacts. |
| `--clips N` | Number of clips (the model is always asked for at least 3). |
| `--provider`, `--model` | `anthropic` (default), `gemini` or `heuristic`, plus a model override. |
| `--whisper`, `--whisper-model` | Force faster-whisper / pick its size. |
| `--force`, `--retranscribe`, `--redetect` | Redo ingest / transcript / highlights. |
| `--variants` | Also render each clip with its alternative hooks (`…_hookB.mp4`, `…_hookC.mp4`) for A/B tests. |
| `--safe-zones` | Overlay the TikTok/Reels/Shorts UI zones (dev only, never for posting). |
| `--only 1,3` | Render only these ranks. |
| `--concurrency N`, `--browser-executable PATH` | Remotion render tuning. `$REMOTION_BROWSER_EXECUTABLE` also works. |

## Tests

```bash
npm test          # TS (trim math, caption re-timing, slugs, edit checks) + Python (json3 parsing, validation, snapping)
npm run typecheck
```

## Project layout

```
scripts/   01_ingest.py  02_transcribe.py  03_detect_viral.py  04_render.ts  run.ts  common.py
remotion/  index.ts  Root.tsx  ViralClip.tsx  schema.ts  fonts.ts
           components/  Captions.tsx  HookOverlay.tsx  ProgressBar.tsx  SafeZoneOverlay.tsx
public/    source.mp4 (ingested, git-ignored)   fonts/ (Montserrat, SIL OFL 1.1)
output/    ingest.json  transcript.json  highlights.json  audio.wav  clips/  (git-ignored)
```

The caption font is bundled in `public/fonts/` so renders don't need network access.

## Roadmap

Built so far, beyond the core pipeline (Tier 1):
- **A/B hook generation:** three variants per clip, rendered with `--variants`.
- **Silence-based snap-to-cut.**
- **Platform safe-zone overlay:** `--safe-zones`.

Next:
- **Tier 2:** face-aware auto-reframing (MediaPipe), contextual B-roll from Pexels/Unsplash, music ducking with `sidechaincompress`.
- **Tier 3:** a review UI with trim handles and an in-browser `<Player>` preview, batch mode over many URLs with a CSV manifest, and confirmed auto-upload through the YouTube Data API and Instagram Graph API.
