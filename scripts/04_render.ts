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
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import {
  buildClipProps,
  checkHighlight,
  matches,
  slugify,
  type BrollPlan,
  type FaceTracks,
  type Highlights,
  type Ingest,
  type Transcript,
} from "../remotion/clip-props.ts";
import { FPS } from "../remotion/schema.ts";
import { CLIPS_DIR, OUTPUT, ROOT, WORKSPACE, ensureWorkspacePublic, rel } from "./paths.ts";
import { renderArgsSpec, renderOptionsFromArgs, type RenderOptions } from "./render-options.ts";

const HIGHLIGHTS = path.join(OUTPUT, "highlights.json");
const TRANSCRIPT = path.join(OUTPUT, "transcript.json");
const INGEST = path.join(OUTPUT, "ingest.json");
const FACE_TRACKS = path.join(OUTPUT, "face_tracks.json");
const BROLL = path.join(OUTPUT, "broll.json");
const PUBLIC = path.join(WORKSPACE, "public");
const SOURCE = path.join(PUBLIC, "source.mp4");
const PYTHON = process.env.PYTHON ?? (process.platform === "win32" ? "python" : "python3");


const VARIANT_LABELS = ["B", "C", "D", "E"];


const readJson = async <T,>(file: string): Promise<T> => JSON.parse(await readFile(file, "utf8")) as T;

export async function renderClips(opts: RenderOptions = {}): Promise<string[]> {
  for (const [file, hint] of [
    [SOURCE, "run the ingest phase first"],
    [HIGHLIGHTS, "run 03_detect_viral.py first"],
    [TRANSCRIPT, "run 02_transcribe.py first"],
  ] as const) {
    if (!existsSync(file)) throw new Error(`${rel(file)} is missing — ${hint}.`);
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
    publicDir: ensureWorkspacePublic(),
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

    const base = `viral_${clip.rank}_${slugify(clip.hook_text)}`;
    const plan = matches(brollPlan[clip.rank], clip) ? brollPlan[clip.rank].items : [];
    const usable = plan.filter((it) => existsSync(path.join(PUBLIC, it.file)));
    const clipProps = (hookText: string) =>
      buildClipProps({
        clip,
        words: allWords,
        ingest,
        track: faceTracks[clip.rank],
        brollItems: usable,
        reframe: reframeMode,
        hookText,
        showSafeZones: opts.safeZones,
      });
    const { props: baseProps, framing } = clipProps(clip.hook_text);
    const durationInFrames = baseProps.clipDurationInFrames;
    const broll = baseProps.broll;
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
      const inputProps = clipProps(hook.text).props;

      const composition = await selectComposition({
        serveUrl,
        id: "ViralClip",
        inputProps,
        browserExecutable: opts.browserExecutable ?? null,
      });

      console.log(
        `[viral-clipper] Rendering #${clip.rank}${hook.label === "A" ? "" : ` (hook ${hook.label})`} ` +
          `[${clip.start.toFixed(2)}s → ${clip.end.toFixed(2)}s, ${durationInFrames} frames] → ${rel(outputLocation)}`,
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

  console.log(`[viral-clipper] Rendered ${rendered.length} file(s) into ${rel(CLIPS_DIR)}/`);
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

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { values } = parseArgs({ options: renderArgsSpec });
  renderClips(renderOptionsFromArgs(values)).catch((err) => {
    console.error(`[viral-clipper] ${err instanceof Error ? err.message : err}`);
    process.exit(1);
  });
}
