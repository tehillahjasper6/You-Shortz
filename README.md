# Viral Clipper

A local-first pipeline that turns a YouTube URL or a video file into ready-to-post **1080×1920** short clips of its most interesting sections, for TikTok, Instagram Reels and YouTube Shorts. Each clip gets burned-in word-by-word captions, an animated hook, a progress bar and platform-specific copy.

```bash
npx tsx scripts/run.ts --url "https://youtube.com/watch?v=..." --clips 3
```

You get:

1. `output/highlights.json`: the chosen sections, most interesting first, which you can edit.
2. `output/clips/viral_{rank}_{hook_slug}.mp4`: H.264 / yuv420p / 30 fps / AAC 192 kbps.
3. `output/clips/viral_{rank}_{hook_slug}.metadata.json` per clip, plus a combined `output/clips/metadata.json`, holding the TikTok description, Reels caption and Shorts title.

`public/source.mp4` is never trimmed. Clips are cut non-destructively at render time, so editing `highlights.json` and re-running only re-renders. Nothing is downloaded or transcribed again.

---

## Setup

Requirements: Node 18+, Python 3.10+.

```bash
npm install                      # Remotion + its bundled FFmpeg/FFprobe
pip install -r requirements.txt  # yt-dlp, faster-whisper, anthropic, opencv, imageio-ffmpeg
export ANTHROPIC_API_KEY=...     # or GEMINI_API_KEY with --provider gemini; not needed with --provider session
export PEXELS_API_KEY=...        # optional: B-roll videos (and/or UNSPLASH_ACCESS_KEY for photos)
```

**Running it from Claude Code on the web:** the repo ships a SessionStart hook (`.claude/hooks/session-start.sh`) that installs everything on session start. `CLAUDE.md` tells Claude how to run the pipeline when you paste a link or upload a video, pick the most interesting sections itself with `--provider session` (no API key), and send the finished clips back in the chat for download. The environment must allow `www.youtube.com`, `*.googlevideo.com`, `huggingface.co` and, for B-roll, `api.pexels.com`, `videos.pexels.com`, `api.unsplash.com` and `images.unsplash.com`. The API keys go in the environment's settings.

You don't need a system FFmpeg. The Python scripts use Remotion's bundled binaries (`npx remotion ffmpeg`). Audio mixing needs filters that build leaves out, so it uses the full static FFmpeg from `imageio-ffmpeg` instead. `$FFMPEG`/`$FFPROBE` or whatever is on `PATH` also work.

## The pipeline

Each phase is a separate script that writes an artifact you can review. A phase is skipped when its artifact already exists.

| Phase | Script | Output |
|---|---|---|
| 1a. Ingest | `scripts/01_ingest.py` | `public/source.mp4`, `output/audio.wav`, `output/ingest.json` |
| 1b. Transcribe | `scripts/02_transcribe.py` | `output/transcript.json` |
| 2. Detect | `scripts/03_detect_viral.py` | `output/highlights.json` |
| 4a. Face tracking | `scripts/04a_face_track.py` | `output/face_tracks.json` |
| 4b. B-roll | `scripts/04b_broll.py` | `output/broll.json`, `public/broll/*` |
| 3–5. Render | `scripts/04_render.ts` | `output/clips/*.mp4` + metadata |
| 5b. Audio master | `scripts/04c_master_audio.py` | in-place on each clip (called by the render) |
| 6. Upload (opt-in) | `scripts/upload.py` | `output/upload_plan.json`, `output/uploads.json` |
| Batch | `scripts/batch.ts` | `workspaces/<video>/…`, `workspaces/batch_manifest.csv` |
| Review UI | `review/` (`npm run review`) | edits `output/highlights.json`, starts renders |
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
The full transcript, with each word tagged by its absolute start time, goes to an LLM using a JSON-schema structured output. It picks **the most interesting and intriguing sections**: a surprising claim, a question that demands an answer, a story with a payoff, a strong opinion. They come back ordered most interesting first, with no scoring rubric. Each section also comes with a hook (≤10 words), three A/B hook variants, why it's interesting, and platform copy.

- **Validation:** `start ≥ 0`, `end ≤ duration`, `end > start`, a length of 15–60 s, a hook of at most 10 words, no overlaps. A response that breaks the contract is retried **once** with the violations listed and a stricter instruction. After that, only the clips that pass on their own are kept.
- **Snap-to-cut:** boundaries move to the nearest phrase edge so a clip never opens or closes mid-word. Start lands 0.2–0.4 s before the first word and end 0.2–0.4 s after the last, preferring a point inside a silence gap found with FFmpeg's `silencedetect`. The model's original times are kept as `llm_start`/`llm_end`. Use `--no-snap` to skip this.
- **Providers:**
  - `--provider anthropic` (default): `claude-sonnet-5-5`, with server-side refusal fallback enabled.
  - `--provider gemini`: `gemini-2.5-flash`, needs `pip install google-genai`.
  - `--provider session`: the Claude running your Claude Code session picks the sections, with no API key.
    - The script writes `output/selection_request.md` (instructions, rules, word-timed transcript and JSON schema) and pauses with exit code 3.
    - Claude writes `output/selection_response.json`, echoing the request id, and re-runs the command.
    - The answer then goes through the same validation, one stricter retry and snapping as an API response. B-roll picking works the same way (`broll_request.md`).
  - `--provider heuristic`: an offline keyword/pace picker for dry runs, no key needed.

### 3–5. Render with Remotion
`remotion/ViralClip.tsx` is one composition, rendered once per highlight with `renderMedia()`:

- **Non-destructive trim:** `<Video>` from `@remotion/media` takes `trimBefore = round(start × 30)` and `durationInFrames = round((end − start) × 30)`. The source file is never pre-cut. If the browser can't decode the file, `@remotion/media` falls back to `<OffthreadVideo>` on its own.
- **Vertical reframe:** the source is fit into the 9:16 frame over a blurred, zoomed copy of itself.
- **Karaoke captions** (`components/Captions.tsx`): pages of 2–4 words built with `@remotion/captions`; the spoken word turns yellow with a small pop. Word times are made clip-relative by subtracting `trimBefore / fps`, not the raw `start`, so captions stay frame-aligned with the video. They sit above the bottom UI zone and clear of the right-hand action rail.
- **Hook overlay** (`components/HookOverlay.tsx`): `hook_text` punches in with `spring()` in the top third for the first 2.5 s.
- **Progress bar** (`components/ProgressBar.tsx`): along the top edge, since every platform draws its own scrubber at the bottom.
- **Loop-friendly ending:** audio fades over the last 0.5 s and the final frame is held for 4 frames, so the loop doesn't cut out abruptly.
- **Output:** H.264, `yuv420p` (BT.709, TV range), 30 fps, AAC 192 kbps.

## Tier 2: production polish

### Face-aware auto-reframing (`04a_face_track.py`)
- **Sampling:** each clip is sampled at 6 fps and faces are found with OpenCV's ResNet-10 SSD face detector. It runs on three overlapping full-height square tiles, so small faces in a wide shot are still found. The model (~5 MB) is downloaded once into `models/` and checksum-verified.
- **Picking the subject:** the camera follows one person. It switches only when another face is at least 1.5× larger for over a second, which is what a cut to the other speaker in a podcast looks like.
- **Moving the camera:** the target is median-filtered. Drift inside a 4% dead zone is ignored; past that, the camera re-centres at no more than 0.45 frame-widths per second. A jump bigger than the crop window becomes a **hard cut** (`cut: true` on the keyframe), not a whip-pan.
- **In the render:** in `track` mode, the source fills the 1920 px height and a 1080 px window slides along the path (`remotion/camera.ts`).
- **When it's used:** `--reframe auto` (the default) tracks when faces are in at least 40% of samples, otherwise it falls back to the blurred-backdrop fit. `--reframe track` and `--reframe fit` force a mode.
- **Caching:** tracks are reused while a clip's start and end are unchanged.

### Contextual B-roll (`04b_broll.py`)
- **Picking moments:** the LLM (or an offline proper-noun heuristic with `--provider heuristic`) picks up to 2 concrete, filmable things per clip, such as "Mount Fuji" or "the new iPhone 16". It returns the time each is spoken and a stock-search query.
- **Fetching footage:** Pexels portrait videos come first (smallest ≥1080p file); Unsplash photos are the fallback, rendered with a slow push-in. Downloads are cached in `public/broll/`, and the credit line is recorded.
- **Timing rules:** no cutaways in the first 3 s (the hook) or the last 2 s (the loop point), and at least 4 s apart.
- **In the render:** a 1.5 s cutaway with a 6-frame cross-dissolve. Audio and captions continue underneath.
- **Editing:** the plan is in `output/broll.json`; move `at`, change `query` or delete items, then re-render. `--rebroll` re-picks. Without `PEXELS_API_KEY`/`UNSPLASH_ACCESS_KEY` the step is skipped.

### Music ducking and loudness mastering (`04c_master_audio.py`)
- **Every clip** gets a two-pass `loudnorm` master to −14 LUFS / −1.5 dBTP after rendering. The video stream is copied untouched.
- **With `--music file.mp3`** (or a folder, which assigns tracks by rank):
  - The track is looped or trimmed to the clip, faded, and set to −20 LUFS.
  - The speech drives `sidechaincompress` on the music. The threshold is calculated from the clip's own speech loudness so the music drops **12 dB** under speech; this was measured on a test clip at 12.7 dB, recovering to about 1 dB within a 0.6 s pause.
  - The mix is then mastered to −14 LUFS.
- **Options:** `--music-lufs` and `--duck-db` tune the levels; `--no-master` skips the step.

## Tier 3: workflow and scale

### Review UI (`npm run review`)
A local web app at http://localhost:5173 for reviewing and editing clips without touching the JSON:
- **Storyboard:** every chosen section as a card with a thumbnail, hook, time range and warnings, ticked to include in a render.
- **Live preview:** Remotion's `<Player>` plays the same `ViralClip` composition the renderer uses, built from the same `buildClipProps()`, so the preview matches the output.
- **Trimming:**
  - Drag the start and end handles over the source timeline; they snap to word boundaries with the same 0.2–0.4 s padding as detection.
  - Or click a transcript word to start there; shift-click to end there.
- **Copy:** edit the hook (or pick one of the A/B variants), the YouTube title (with a 100-character counter), the TikTok description and the Reels caption.
- **Save and render:** **Save** validates and writes `highlights.json`. **Render selected** runs the pipeline for the ticked clips (face tracking, B-roll, render, master), shows progress, and links the finished MP4s.

To review a batch video, point it at that video's workspace: `VIRAL_CLIPPER_WORKSPACE=workspaces/<id> npm run review`. The UI is a local dev server, so it runs on your own machine, not inside a Claude Code cloud session.

### Batch mode (`npm run batch`)
```bash
npx tsx scripts/batch.ts --urls list.txt --clips 3 --cleanup
npx tsx scripts/batch.ts --url "https://youtube.com/@creator" --limit 20 -- --music bed.mp3
```
- **Inputs:** video, playlist and channel URLs (channels and playlists expand to their videos, newest first, up to `--limit`) and local files, one per line, with `#` comments allowed.
- **Running:** videos are processed one after another, each in its own workspace `workspaces/<video id>/`, so they never overwrite each other. Arguments after `--` go to `run.ts` for every video.
- **Resuming:** videos that already have clips are skipped, so an interrupted batch picks up where it stopped; `--force` redoes them.
- **Disk:** `--cleanup` deletes each video's source and intermediate media once its clips are rendered.
- **Results:** `workspaces/batch_manifest.csv` (and `.json`) lists every clip from every video (each video's clips most interesting first), with its titles, captions and file path. Failed videos are listed at the end with the reason, and the command exits non-zero.

Any single command can target a workspace with `--workspace DIR` or `$VIRAL_CLIPPER_WORKSPACE`.

### Auto-upload, gated (`scripts/upload.py`)
Nothing is posted without an explicit confirmation step:
```bash
python3 scripts/upload.py plan --platforms youtube,instagram --privacy private
#   prints each clip's destination, title/caption and privacy, plus a confirmation code
python3 scripts/upload.py run --confirm 3f9a1c2e
```
- **The confirmation code** is a hash of the plan and of the clip files. A plan edited after review, or a clip re-rendered since, needs a fresh `plan` and a fresh code.
- **YouTube Shorts:** YouTube Data API v3 resumable upload, with title, description (hook, hashtags, `#Shorts`, B-roll credits), tags and privacy (default **private**). Needs `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET` and `YOUTUBE_REFRESH_TOKEN`, an OAuth refresh token with the `youtube.upload` scope.
- **Instagram Reels:** Instagram Graph API container with resumable upload, then a status poll, then publish. Needs `IG_USER_ID` and `IG_ACCESS_TOKEN` (`instagram_content_publish`). Instagram has no private option, so publishing makes the Reel public, and the plan says so.
- **Re-runs:** every upload is logged in `output/uploads.json`, and a re-run skips anything already posted.
- **Network:** uploads need `oauth2.googleapis.com`, `www.googleapis.com`, `graph.facebook.com` and `rupload.facebook.com`.
- **TikTok** isn't supported: its Content Posting API requires an audited app. Use the TikTok description in `metadata.json` when posting by hand.

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
| `--reframe auto\|track\|fit` | Face-tracking crop (default `auto`). |
| `--no-broll`, `--broll-per-clip N`, `--rebroll` | B-roll cutaways. |
| `--music PATH`, `--music-lufs N`, `--duck-db N`, `--no-master` | Music bed, ducking and the −14 LUFS master. |
| `--cookies FILE` | cookies.txt for YouTube videos that ask you to sign in (common from cloud servers). |
| `--concurrency N`, `--browser-executable PATH` | Remotion render tuning. `$REMOTION_BROWSER_EXECUTABLE` also works. |

## Tests

```bash
npm test          # TS (trim math, captions, camera path, reframe/B-roll, snap handles, batch CSV) + Python (json3, validation, snapping, face tracking, B-roll, ducking, upload gating)
npm run typecheck
```

## Project layout

```
scripts/   01_ingest.py  02_transcribe.py  03_detect_viral.py  04a_face_track.py  04b_broll.py
           04c_master_audio.py  04_render.ts  run.ts  batch.ts  expand_urls.py  upload.py
           common.py  paths.ts  render-options.ts
remotion/  index.ts  Root.tsx  ViralClip.tsx  schema.ts  clip-props.ts  camera.ts  fonts.ts
           components/  Captions.tsx  HookOverlay.tsx  ProgressBar.tsx  SafeZoneOverlay.tsx  BRoll.tsx
public/    source.mp4 (ingested, git-ignored)   fonts/ (Montserrat, SIL OFL 1.1)
review/    vite.config.ts  api.ts  index.html  src/  (the review UI)
output/    ingest.json  transcript.json  highlights.json  face_tracks.json  broll.json  audio.wav  clips/  (git-ignored)
workspaces/  one directory per batch video + batch_manifest.csv  (git-ignored)
models/    face detector weights, downloaded on first use (git-ignored)
```

The caption font is bundled in `public/fonts/` so renders don't need network access.

## Roadmap

Built so far, beyond the core pipeline (Tier 1):
- **A/B hook generation:** three variants per clip, rendered with `--variants`.
- **Silence-based snap-to-cut.**
- **Platform safe-zone overlay:** `--safe-zones`.

Tier 2 (built): face-aware auto-reframing, contextual B-roll, music ducking with a −14 LUFS master.

Tier 3 (built): the review UI, batch mode with a CSV manifest of every clip, and gated auto-upload to YouTube Shorts and Instagram Reels.
