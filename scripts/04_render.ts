/**
 * Phase 3-5 — Render every highlights.json entry with Remotion.
 *
 * Each clip is a ViralClip composition that trims public/source.mp4
 * non-destructively (trimBefore + durationInFrames), so editing
 * highlights.json and re-running this script re-renders without touching the
 * source, the download or the transcript.
 *
 *   npx tsx scripts/04_render.ts [--only 1,3] [--variants] [--safe-zones]
 *
 * Writes output/clips/viral_{rank}_{hook_slug}.mp4, a sidecar
 * viral_{rank}_{hook_slug}.metadata.json per clip, and output/clips/metadata.json.
 */
import { bundle } from "@remotion/bundler";
import { renderMedia, selectComposition } from "@remotion/renderer";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { FPS, defaultViralClipProps, type ClipWord, type ViralClipProps } from "../remotion/schema.ts";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OUTPUT = path.join(ROOT, "output");
const CLIPS_DIR = path.join(OUTPUT, "clips");
const HIGHLIGHTS = path.join(OUTPUT, "highlights.json");
const TRANSCRIPT = path.join(OUTPUT, "transcript.json");
const SOURCE = path.join(ROOT, "public", "source.mp4");

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

export type RenderOptions = {
  only?: number[];
  variants?: boolean;
  safeZones?: boolean;
  concurrency?: number | null;
  browserExecutable?: string | null;
};

const MIN_CLIP_S = 15;
const MAX_CLIP_S = 60;
const VARIANT_LABELS = ["B", "C", "D", "E"];

const MAX_SLUG = 40;

/** Filename-safe slug of the hook, cut at a word boundary. */
export const slugify = (text: string): string => {
  const slug = text
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
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

export const renderArgsSpec = {
  only: { type: "string" },
  variants: { type: "boolean", default: false },
  "safe-zones": { type: "boolean", default: false },
  concurrency: { type: "string" },
  "browser-executable": { type: "string" },
} as const;

export const renderOptionsFromArgs = (values: {
  only?: string;
  variants?: boolean;
  "safe-zones"?: boolean;
  concurrency?: string;
  "browser-executable"?: string;
}): RenderOptions => ({
  only: values.only ? values.only.split(",").map((n) => Number.parseInt(n, 10)).filter(Number.isFinite) : undefined,
  variants: values.variants,
  safeZones: values["safe-zones"],
  concurrency: values.concurrency ? Number.parseInt(values.concurrency, 10) : null,
  browserExecutable: values["browser-executable"] ?? process.env.REMOTION_BROWSER_EXECUTABLE ?? null,
});

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { values } = parseArgs({ options: renderArgsSpec });
  renderClips(renderOptionsFromArgs(values)).catch((err) => {
    console.error(`[viral-clipper] ${err instanceof Error ? err.message : err}`);
    process.exit(1);
  });
}
