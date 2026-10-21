/**
 * One command, all phases:
 *
 *   npx tsx scripts/run.ts --url "https://youtube.com/watch?v=..." --clips 3
 *   npx tsx scripts/run.ts --file talk.mp4 --clips 5 --variants
 *   npx tsx scripts/run.ts                     # re-render from the existing highlights.json
 *
 * Every phase leaves a reviewable artifact and is skipped when that artifact
 * already exists, so re-running after editing output/highlights.json goes
 * straight to rendering — no re-download, no re-transcription.
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { renderArgsSpec, renderClips, renderOptionsFromArgs } from "./04_render.ts";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PYTHON = process.env.PYTHON ?? (process.platform === "win32" ? "python" : "python3");

const { values } = parseArgs({
  options: {
    url: { type: "string" },
    file: { type: "string" },
    clips: { type: "string", default: "3" },
    provider: { type: "string", default: "anthropic" },
    model: { type: "string" },
    "sub-lang": { type: "string" },
    cookies: { type: "string" },
    whisper: { type: "boolean", default: false },
    "whisper-model": { type: "string" },
    force: { type: "boolean", default: false },
    retranscribe: { type: "boolean", default: false },
    redetect: { type: "boolean", default: false },
    "broll-per-clip": { type: "string" },
    rebroll: { type: "boolean", default: false },
    "no-render": { type: "boolean", default: false },
    help: { type: "boolean", short: "h", default: false },
    ...renderArgsSpec,
  },
});

if (values.help) {
  console.log(`Usage: npx tsx scripts/run.ts (--url URL | --file PATH) [options]

Source
  --url URL              YouTube video to ingest
  --file PATH            Local .mp4 / .mov / .mkv
  --force                Replace public/source.mp4 when switching to a new source
  --sub-lang LANG        YouTube subtitle language (default en)
  --cookies FILE         cookies.txt for YouTube when it asks to sign in (or $YTDLP_COOKIES)

Transcription
  --whisper              Ignore YouTube subtitles; always use faster-whisper
  --whisper-model SIZE   tiny | base | small (default) | medium | large-v3
  --retranscribe         Redo transcript.json

Detection
  --clips N              Number of clips (default 3)
  --provider NAME        anthropic (default) | gemini | heuristic (offline)
  --model ID             Override the provider's model
  --redetect             Overwrite highlights.json (discards your edits)

Enhancement (Tier 2)
  --reframe MODE         auto (default: follow faces when found) | track | fit
  --no-broll             Skip B-roll cutaways (they also need PEXELS_API_KEY or UNSPLASH_ACCESS_KEY)
  --broll-per-clip N     Maximum cutaways per clip (default 2)
  --rebroll              Re-pick and re-fetch B-roll (discards edits to broll.json)
  --music PATH           Music file or folder: ducked 12 dB under speech
  --music-lufs N         Music bed loudness before ducking (default -20)
  --duck-db N            How far the music drops under speech (default 12)
  --no-master            Skip the final -14 LUFS audio master

Rendering
  --no-render            Stop after highlights.json so you can review it
  --only 1,3             Render only these ranks
  --variants             Also render A/B hook variants
  --safe-zones           Draw platform UI safe-zone guides (dev only)
  --concurrency N        Remotion render concurrency
  --browser-executable P Chrome/Chromium to render with`);
  process.exit(0);
}

const python = (script: string, args: string[]) => {
  const res = spawnSync(PYTHON, [path.join(ROOT, "scripts", script), ...args], { stdio: "inherit", cwd: ROOT });
  if (res.error) throw new Error(`Could not run ${PYTHON}: ${res.error.message} (set $PYTHON to your interpreter)`);
  if (res.status !== 0) process.exit(res.status ?? 1);
};

const main = async () => {
  const opt = (flag: string, value: string | undefined) => (value ? [flag, value] : []);

  if (values.url || values.file) {
    python("01_ingest.py", [
      ...opt("--url", values.url),
      ...opt("--file", values.file),
      ...opt("--sub-lang", values["sub-lang"]),
      ...opt("--cookies", values.cookies),
      ...(values.force ? ["--force"] : []),
    ]);
  } else if (!existsSync(path.join(ROOT, "public", "source.mp4"))) {
    console.error("Nothing ingested yet: pass --url or --file.");
    process.exit(1);
  }

  python("02_transcribe.py", [
    ...(values.whisper ? ["--whisper"] : []),
    ...opt("--model", values["whisper-model"]),
    ...(values.retranscribe ? ["--force"] : []),
  ]);

  python("03_detect_viral.py", [
    "--clips", values.clips ?? "3",
    "--provider", values.provider ?? "anthropic",
    ...opt("--model", values.model),
    ...(values.redetect || values.retranscribe ? ["--force"] : []),
  ]);

  if (values["no-render"]) {
    console.log("[viral-clipper] Review output/highlights.json, then run `npm run render`.");
    return;
  }

  const renderOptions = renderOptionsFromArgs(values);
  const only = values.only ? ["--only", values.only] : [];
  if (renderOptions.reframe !== "fit") python("04a_face_track.py", only);
  if (renderOptions.broll) {
    python("04b_broll.py", [
      "--provider", values.provider ?? "anthropic",
      ...opt("--model", values.model),
      ...opt("--per-clip", values["broll-per-clip"]),
      ...(values.rebroll ? ["--force"] : []),
      ...only,
    ]);
  }
  await renderClips(renderOptions);
};

main().catch((err) => {
  console.error(`[viral-clipper] ${err instanceof Error ? err.message : err}`);
  process.exit(1);
});
