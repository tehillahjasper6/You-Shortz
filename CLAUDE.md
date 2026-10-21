# Viral Clipper: notes for Claude

The owner runs this pipeline from Claude Code cloud sessions. They paste a YouTube link or upload a video and expect the finished clips back in the chat. See README.md for the full pipeline design.

## When given a YouTube link or an uploaded video

1. Run the whole pipeline in one command from the repo root:
   - YouTube: `npx tsx scripts/run.ts --url "<link>" --clips 3`
   - Upload: `npx tsx scripts/run.ts --file "<path of the uploaded file>" --clips 3`
   - A source that differs from the one already in `public/source.mp4` needs `--force`. Add it when the user gives a new video; it moves the old transcript and highlights aside.
   - Add the owner's options as asked: `--clips N`, `--music <file|dir>`, `--variants`, `--reframe fit`, `--no-broll`, `--whisper`.
2. Rendering takes minutes per clip. Run it in the background and tell the user it's running.
3. Deliver with `SendUserFile`: each `output/clips/viral_*.mp4`, plus `output/clips/metadata.json` (platform captions and titles). Summarize each clip in one line: rank, score, hook and time range.
4. Edits ("make clip 2 start earlier", "change the hook"): edit `output/highlights.json`, then `npm run render -- --only <rank>`. Never re-download or re-transcribe for an edit.

## Things that block a run (tell the owner, don't work around them)

- **Network:** `www.youtube.com` and `*.googlevideo.com` (download), `huggingface.co` (Whisper models), `api.pexels.com` / `videos.pexels.com` / `api.unsplash.com` / `images.unsplash.com` (B-roll). These must be allowed in the environment's network settings.
- **Secrets:** `ANTHROPIC_API_KEY` (scoring), `PEXELS_API_KEY` and/or `UNSPLASH_ACCESS_KEY` (B-roll, optional). These go in environment variables, never pasted into chat.
- **YouTube bot check** ("Sign in to confirm you're not a bot") on cloud IPs: needs `--cookies <cookies.txt>`, or have the owner upload the video file instead.
- Without an Anthropic key, `--provider heuristic` gives an offline dry run. Say that its picks are keyword-based.

## Conventions

- `output/`, `public/source.mp4`, `public/broll/` and `models/` are runtime artifacts and are git-ignored. Never commit them.
- Tests: `npm test` (TypeScript and Python). Typecheck: `npm run typecheck`.
- Remotion's bundled FFmpeg lacks audio-mixing filters. `scripts/common.py: ffmpeg_cmd(filters)` picks a build that has the ones a job needs (imageio-ffmpeg from pip).
