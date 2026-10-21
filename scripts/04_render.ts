/**
 * Phase 3-5 — Render every highlights.json entry with Remotion.
 *
 * Each clip is a ViralClip composition that trims public/source.mp4
 * non-destructively (trimBefore + durationInFrames), so editing
 * highlights.json and re-running this script re-renders without touching the
 * source, the download or the transcript.
 *
 *   npx tsx scripts/04_render.ts [--only 1,3] [--variants] [--safe-zones]
 *                                [--reframe auto|track|fit] [--no-broll] [--music PATH]
 *
 * Uses output/face_tracks.json (04a) and output/broll.json (04b) when their
 * entries match a clip's current start/end, then masters each render's audio
 * (music ducking + -14 LUFS) with 04c_master_audio.py.
 *
 * Writes output/clips/viral_{rank}_{hook_slug}.mp4, a sidecar
 * viral_{rank}_{hook_slug}.metadata.json per clip, and output/clips/metadata.json.
 */
import { bundle } from "@remotion/bundler";
import { renderMedia, selectComposition } from "@remotion/renderer";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import {
  FPS,
  defaultViralClipProps,
  type BrollItem,
  type CameraKeyframe,
  type ClipWord,
  type ViralClipProps,
} from "../remotion/schema.ts";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OUTPUT = path.join(ROOT, "output");
const CLIPS_DIR = path.join(OUTPUT, "clips");
const HIGHLIGHTS = path.join(OUTPUT, "highlights.json");
const TRANSCRIPT = path.join(OUTPUT, "transcript.json");
const INGEST = path.join(OUTPUT, "ingest.json");
const FACE_TRACKS = path.join(OUTPUT, "face_tracks.json");
const BROLL = path.join(OUTPUT, "broll.json");
const SOURCE = path.join(ROOT, "public", "source.mp4");
const PYTHON = process.env.PYTHON ?? (process.platform === "win32" ? "python" : "python3");

export type Highlight = {
  rank: number;
  start: number;
  end: number;
  viral_score: number;
  hook_text: string;
  reasoning: string;
  scores?: Record<string, number>;
  hook_variants?: { text: string; predicted_ctr: number }[];
  platform?: { tiktok_description?: string; instagram_caption?: string; youtube_title?: string };
};

type Highlights = { video_duration?: number; fps?: number; title?: string | null; clips: Highlight[] };
type Transcript = { duration: number; segments: { words: ClipWord[] }[] };
type Ingest = { width?: number; height?: number };

export type FaceTrack = {
  start: number;
  end: number;
  frame_width: number;
  frame_height: number;
  mode: "track" | "none";
  reason: string;
  keyframes: CameraKeyframe[];
};
type FaceTracks = { clips: Record<string, FaceTrack> };

export type BrollPlanItem = {
  at: number;
  duration: number;
  phrase: string;
  query: string;
  type: "video" | "image";
  file: string;
  credit: string;
  source_url?: string;
};
type BrollPlan = { clips: Record<string, { start: number; end: number; items: BrollPlanItem[] }> };

export type Reframe = "auto" | "track" | "fit";

export type RenderOptions = {
  only?: number[];
  variants?: boolean;
  safeZones?: boolean;
  concurrency?: number | null;
  browserExecutable?: string | null;
  reframe?: Reframe;
  broll?: boolean;
  master?: boolean;
  music?: string | null;
  musicLufs?: number | null;
  duckDb?: number | null;
};

const MIN_CLIP_S = 15;
const MAX_CLIP_S = 60;
const VARIANT_LABELS = ["B", "C", "D", "E"];

const MAX_SLUG = 40;

/** Filename-safe slug of the hook, cut at a word boundary. */
export const slugify = (text: string): string => {
  const slug = text
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (slug.length <= MAX_SLUG) return slug || "clip";
  const cut = slug.slice(0, MAX_SLUG + 1);
  const lastDash = cut.lastIndexOf("-");
  return lastDash > 0 ? cut.slice(0, lastDash) : slug.slice(0, MAX_SLUG);
};

/** Frame-accurate trim for a clip: trimBefore = round(start * fps), duration = round((end - start) * fps). */
export const clipFrames = (start: number, end: number, fps = FPS) => ({
  trimBefore: Math.round(start * fps),
  durationInFrames: Math.round((end - start) * fps),
});

/**
 * Words overlapping [start, end], re-timed relative to the clip's first frame.
 * Subtracts trimBefore / fps (not the raw start) so captions line up with the
 * frame the video actually starts on.
 */
export const clipWords = (words: ClipWord[], start: number, end: number, fps = FPS): ClipWord[] => {
  const offset = Math.round(start * fps) / fps;
  return words
    .filter((w) => w.end > start && w.start < end)
    .map((w) => ({
      word: w.word,
      start: Math.max(0, +(w.start - offset).toFixed(3)),
      end: +(Math.min(w.end, end) - offset).toFixed(3),
    }));
};

/** A cached artifact entry applies only while the clip's boundaries are unchanged. */
const matches = (entry: { start: number; end: number } | undefined, h: Highlight) =>
  Boolean(entry && Math.abs(entry.start - h.start) < 1e-6 && Math.abs(entry.end - h.end) < 1e-6);

/** Decide fit vs track for a clip. "auto" tracks only where the face tracker found a subject. */
export const chooseReframe = (
  mode: Reframe,
  track: FaceTrack | undefined,
): { reframe: "fit" | "track"; cameraPath: CameraKeyframe[]; reason: string } => {
  if (mode === "fit") return { reframe: "fit", cameraPath: [], reason: "fit requested" };
  if (!track) return { reframe: "fit", cameraPath: [], reason: "no face track for these boundaries" };
  if (mode === "auto" && track.mode !== "track") return { reframe: "fit", cameraPath: [], reason: track.reason };
  if (!track.keyframes.length) return { reframe: "fit", cameraPath: [], reason: "face track is empty" };
  return { reframe: "track", cameraPath: track.keyframes, reason: track.reason };
};

/** B-roll plan items re-timed to the clip, keeping only cutaways that fit inside it. */
export const clipBroll = (items: BrollPlanItem[], start: number, end: number, fps = FPS): BrollItem[] => {
  const offset = Math.round(start * fps) / fps;
  return items
    .filter((it) => it.at >= start && it.at + it.duration <= end)
    .map((it) => ({ from: +(it.at - offset).toFixed(3), duration: it.duration, src: it.file, type: it.type }));
};

/** Problems that make a (possibly hand-edited) highlight unrenderable, plus soft warnings. */
export const checkHighlight = (h: Highlight, duration: number): { errors: string[]; warnings: string[] } => {
  const errors: string[] = [];
  const warnings: string[] = [];
  if (!(typeof h.start === "number" && typeof h.end === "number")) errors.push("start/end must be numbers");
  else {
    if (h.start < 0) errors.push(`start ${h.start} is negative`);
    if (h.end > duration + 1e-3) errors.push(`end ${h.end} is past the video duration ${duration}`);
    if (h.end <= h.start) errors.push(`end ${h.end} is not after start ${h.start}`);
    const len = h.end - h.start;
    if (len > 0 && (len < MIN_CLIP_S || len > MAX_CLIP_S)) warnings.push(`duration ${len.toFixed(2)}s is outside ${MIN_CLIP_S}-${MAX_CLIP_S}s`);
  }
  if ((h.hook_text ?? "").trim().split(/\s+/).length > 10) warnings.push("hook_text is longer than 10 words");
  return { errors, warnings };
};

const readJson = async <T,>(file: string): Promise<T> => JSON.parse(await readFile(file, "utf8")) as T;

export async function renderClips(opts: RenderOptions = {}): Promise<string[]> {
  for (const [file, hint] of [
    [SOURCE, "run the ingest phase first"],
    [HIGHLIGHTS, "run 03_detect_viral.py first"],
    [TRANSCRIPT, "run 02_transcribe.py first"],
  ] as const) {
    if (!existsSync(file)) throw new Error(`${path.relative(ROOT, file)} is missing — ${hint}.`);
  }

  const highlights = await readJson<Highlights>(HIGHLIGHTS);
  const transcript = await readJson<Transcript>(TRANSCRIPT);
  const duration = highlights.video_duration ?? transcript.duration;
  const allWords = transcript.segments.flatMap((s) => s.words);
  const ingest = existsSync(INGEST) ? await readJson<Ingest>(INGEST) : {};
  const faceTracks = existsSync(FACE_TRACKS) ? (await readJson<FaceTracks>(FACE_TRACKS)).clips : {};
  const brollPlan = opts.broll !== false && existsSync(BROLL) ? (await readJson<BrollPlan>(BROLL)).clips : {};
  const reframeMode = opts.reframe ?? "auto";

  const selected = highlights.clips.filter((c) => !opts.only?.length || opts.only.includes(c.rank));
  if (!selected.length) throw new Error("No clips selected to render.");

  console.log(`[viral-clipper] Bundling Remotion project…`);
  const serveUrl = await bundle({
    entryPoint: path.join(ROOT, "remotion", "index.ts"),
    publicDir: path.join(ROOT, "public"),
    onProgress: () => undefined,
  });

  await mkdir(CLIPS_DIR, { recursive: true });
  const rendered: string[] = [];
  const manifest: unknown[] = [];

  for (const clip of selected) {
    const { errors, warnings } = checkHighlight(clip, duration);
    warnings.forEach((w) => console.warn(`[viral-clipper] clip #${clip.rank}: warning — ${w}`));
    if (errors.length) {
      console.error(`[viral-clipper] clip #${clip.rank}: skipped — ${errors.join("; ")}`);
      continue;
    }

    const { trimBefore, durationInFrames } = clipFrames(clip.start, clip.end);
    const words = clipWords(allWords, clip.start, clip.end);
    const base = `viral_${clip.rank}_${slugify(clip.hook_text)}`;

    const track = matches(faceTracks[clip.rank], clip) ? faceTracks[clip.rank] : undefined;
    const framing = chooseReframe(reframeMode, track);
    const sourceWidth = ingest.width ?? track?.frame_width ?? 0;
    const sourceHeight = ingest.height ?? track?.frame_height ?? 0;
    const plan = matches(brollPlan[clip.rank], clip) ? brollPlan[clip.rank].items : [];
    const usable = plan.filter((it) => existsSync(path.join(ROOT, "public", it.file)));
    const broll = clipBroll(usable, clip.start, clip.end);
    console.log(
      `[viral-clipper] clip #${clip.rank}: reframe=${framing.reframe} (${framing.reason}), ${broll.length} B-roll cutaway(s)`,
    );

    const hooks: { label: string; text: string; predicted_ctr?: number }[] = [{ label: "A", text: clip.hook_text }];
    if (opts.variants) {
      const extra = (clip.hook_variants ?? []).filter(
        (v) => v.text.trim() && v.text.trim().toLowerCase() !== clip.hook_text.trim().toLowerCase(),
      );
      extra.slice(0, VARIANT_LABELS.length).forEach((v, i) => hooks.push({ label: VARIANT_LABELS[i], text: v.text, predicted_ctr: v.predicted_ctr }));
    }

    const files: { hook: string; label: string; file: string; predicted_ctr?: number }[] = [];
    for (const hook of hooks) {
      const name = hook.label === "A" ? `${base}.mp4` : `${base}_hook${hook.label}.mp4`;
      const outputLocation = path.join(CLIPS_DIR, name);
      const inputProps: ViralClipProps = {
        ...defaultViralClipProps,
        src: "source.mp4",
        trimBefore,
        clipDurationInFrames: durationInFrames,
        words,
        hookText: hook.text,
        reframe: framing.reframe,
        sourceWidth,
        sourceHeight,
        cameraPath: framing.cameraPath,
        broll,
        showSafeZones: Boolean(opts.safeZones),
      };

      const composition = await selectComposition({
        serveUrl,
        id: "ViralClip",
        inputProps,
        browserExecutable: opts.browserExecutable ?? null,
      });

      console.log(
        `[viral-clipper] Rendering #${clip.rank}${hook.label === "A" ? "" : ` (hook ${hook.label})`} ` +
          `[${clip.start.toFixed(2)}s → ${clip.end.toFixed(2)}s, ${durationInFrames} frames] → ${path.relative(ROOT, outputLocation)}`,
      );
      let lastPct = -10;
      await renderMedia({
        serveUrl,
        composition,
        inputProps,
        codec: "h264",
        pixelFormat: "yuv420p",
        // BT.709 + TV range; without it the JPEG frame path yields full-range yuvj420p.
        colorSpace: "bt709",
        audioCodec: "aac",
        audioBitrate: "192k",
        imageFormat: "jpeg",
        jpegQuality: 90,
        outputLocation,
        overwrite: true,
        concurrency: opts.concurrency ?? null,
        browserExecutable: opts.browserExecutable ?? null,
        onProgress: ({ progress }) => {
          const pct = Math.floor(progress * 100);
          if (pct >= lastPct + 10) {
            lastPct = pct;
            process.stdout.write(`  ${pct}%\n`);
          }
        },
      });
      rendered.push(outputLocation);
      files.push({ hook: hook.text, label: hook.label, file: name, predicted_ctr: hook.predicted_ctr });
    }

    const audio = opts.master === false ? null : masterAudio(files.map((f) => path.join(CLIPS_DIR, f.file)), clip.rank, opts);

    const meta = {
      rank: clip.rank,
      file: `${base}.mp4`,
      start: clip.start,
      end: clip.end,
      duration: +(clip.end - clip.start).toFixed(3),
      viral_score: clip.viral_score,
      scores: clip.scores,
      hook_text: clip.hook_text,
      reasoning: clip.reasoning,
      tiktok: { description: clip.platform?.tiktok_description ?? "" },
      instagram_reels: { caption: clip.platform?.instagram_caption ?? "" },
      youtube_shorts: { title: (clip.platform?.youtube_title ?? clip.hook_text).slice(0, 100) },
      hook_variants: files,
      reframe: { mode: framing.reframe, reason: framing.reason },
      broll: plan
        .filter((it) => broll.some((b) => b.src === it.file))
        .map((it) => ({ at: it.at, phrase: it.phrase, query: it.query, file: it.file, credit: it.credit, source_url: it.source_url })),
      audio: audio ?? { mastered: false },
      render: { width: 1080, height: 1920, fps: FPS, codec: "h264", pixel_format: "yuv420p", audio: "aac 192k" },
    };
    await writeFile(path.join(CLIPS_DIR, `${base}.metadata.json`), JSON.stringify(meta, null, 2) + "\n");
    manifest.push(meta);
  }

  // Merge into the existing manifest so `--only` renders don't drop other clips.
  const manifestPath = path.join(CLIPS_DIR, "metadata.json");
  const previous = existsSync(manifestPath) ? (await readJson<{ clips?: { rank: number }[] }>(manifestPath)).clips ?? [] : [];
  const byRank = new Map<number, unknown>(previous.map((c) => [c.rank, c]));
  for (const m of manifest as { rank: number }[]) byRank.set(m.rank, m);
  const ranks = new Set(highlights.clips.map((c) => c.rank));
  await writeFile(
    manifestPath,
    JSON.stringify(
      {
        title: highlights.title ?? null,
        generated_at: new Date().toISOString(),
        clips: [...byRank.entries()].filter(([r]) => ranks.has(r)).sort(([a], [b]) => a - b).map(([, m]) => m),
      },
      null,
      2,
    ) + "\n",
  );

  console.log(`[viral-clipper] Rendered ${rendered.length} file(s) into ${path.relative(ROOT, CLIPS_DIR)}/`);
  return rendered;
}

/**
 * Music bed + ducking + -14 LUFS master, in place, via 04c_master_audio.py.
 * Rendering already succeeded, so a failure here warns rather than aborting.
 */
const masterAudio = (files: string[], rank: number, opts: RenderOptions): Record<string, unknown> | null => {
  const report = path.join(OUTPUT, "work", `master_${rank}.json`);
  const args = [path.join(ROOT, "scripts", "04c_master_audio.py"), "--report", report];
  for (const f of files) args.push("--clip", f, "--rank", String(rank));
  if (opts.music) args.push("--music", opts.music);
  if (opts.musicLufs != null) args.push("--music-lufs", String(opts.musicLufs));
  if (opts.duckDb != null) args.push("--duck-db", String(opts.duckDb));
  const res = spawnSync(PYTHON, args, { stdio: ["ignore", "inherit", "inherit"], cwd: ROOT });
  if (res.status !== 0) {
    console.warn(`[viral-clipper] clip #${rank}: audio mastering failed; keeping the unmastered render.`);
    return null;
  }
  try {
    const reports = JSON.parse(readFileSync(report, "utf8")) as Record<string, Record<string, unknown>>;
    const first = reports[path.basename(files[0])] ?? {};
    return {
      mastered: true,
      target_lufs: first.target_lufs,
      ...(first.music ? { music: path.basename(String(first.music)), duck_db: first.duck_db } : {}),
    };
  } catch {
    return { mastered: true };
  }
};

export const renderArgsSpec = {
  only: { type: "string" },
  variants: { type: "boolean", default: false },
  "safe-zones": { type: "boolean", default: false },
  concurrency: { type: "string" },
  "browser-executable": { type: "string" },
  reframe: { type: "string", default: "auto" },
  "no-broll": { type: "boolean", default: false },
  "no-master": { type: "boolean", default: false },
  music: { type: "string" },
  "music-lufs": { type: "string" },
  "duck-db": { type: "string" },
} as const;

const REFRAMES: Reframe[] = ["auto", "track", "fit"];

export const renderOptionsFromArgs = (values: {
  only?: string;
  variants?: boolean;
  "safe-zones"?: boolean;
  concurrency?: string;
  "browser-executable"?: string;
  reframe?: string;
  "no-broll"?: boolean;
  "no-master"?: boolean;
  music?: string;
  "music-lufs"?: string;
  "duck-db"?: string;
}): RenderOptions => {
  const reframe = (values.reframe ?? "auto") as Reframe;
  if (!REFRAMES.includes(reframe)) throw new Error(`--reframe must be one of ${REFRAMES.join(", ")}`);
  const num = (v?: string) => (v == null ? null : Number.parseFloat(v));
  return {
    only: values.only ? values.only.split(",").map((n) => Number.parseInt(n, 10)).filter(Number.isFinite) : undefined,
    variants: values.variants,
    safeZones: values["safe-zones"],
    concurrency: values.concurrency ? Number.parseInt(values.concurrency, 10) : null,
    browserExecutable: values["browser-executable"] ?? process.env.REMOTION_BROWSER_EXECUTABLE ?? null,
    reframe,
    broll: !values["no-broll"],
    master: !values["no-master"],
    music: values.music ? path.resolve(values.music) : null,
    musicLufs: num(values["music-lufs"]),
    duckDb: num(values["duck-db"]),
  };
};

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { values } = parseArgs({ options: renderArgsSpec });
  renderClips(renderOptionsFromArgs(values)).catch((err) => {
    console.error(`[viral-clipper] ${err instanceof Error ? err.message : err}`);
    process.exit(1);
  });
}
