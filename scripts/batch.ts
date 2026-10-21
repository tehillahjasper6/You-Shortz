/**
 * Batch mode: run the whole pipeline over many videos (e.g. a creator's back
 * catalog), one after another, each in its own workspace, then list every
 * clip from every video in one CSV manifest.
 *
 *   npx tsx scripts/batch.ts --urls list.txt --clips 3
 *   npx tsx scripts/batch.ts --url https://youtube.com/@creator --limit 20 -- --music bed.mp3
 *
 * Inputs: video URLs, playlist/channel URLs (expanded, newest first, up to
 * --limit each) and local video files; list files take one per line, with #
 * comments. Arguments after `--` are passed to scripts/run.ts for every video.
 *
 * Each video lives in workspaces/<video id>/{public,output}. Videos whose
 * clips already exist are skipped, so an interrupted batch resumes where it
 * stopped (--force redoes them). --cleanup deletes each video's source and
 * intermediate media once its clips are rendered, to save disk.
 *
 * Writes workspaces/batch_manifest.csv (all clips, per video in pick order) and
 * workspaces/batch_manifest.json.
 */
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, rmSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
// Exit code the pipeline uses while waiting for the Claude Code session (--provider session).
const AWAITING_SESSION = 3;
const PYTHON = process.env.PYTHON ?? (process.platform === "win32" ? "python" : "python3");

export type BatchVideo = { source: "url" | "file"; url?: string; path?: string; id: string; title?: string | null; error?: string };

type ClipMeta = {
  rank: number;
  file: string;
  start: number;
  end: number;
  duration: number;
  hook_text: string;
  tiktok?: { description?: string };
  instagram_reels?: { caption?: string };
  youtube_shorts?: { title?: string };
};

export type BatchResult = {
  video: BatchVideo;
  workspace: string;
  status: "done" | "skipped" | "failed";
  error?: string;
  title?: string | null;
  clips: ClipMeta[];
};

export const MANIFEST_COLUMNS = [
  "video_title",
  "video_source",
  "clip_rank",
  "start",
  "end",
  "duration",
  "hook_text",
  "youtube_title",
  "tiktok_description",
  "instagram_caption",
  "clip_file",
  "status",
] as const;

const csvCell = (v: unknown): string => {
  const s = v == null ? "" : String(v);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

/** One row per clip: videos in batch order, each video's clips most interesting first; failed videos last. */
export const buildManifestRows = (results: BatchResult[], root: string): Record<(typeof MANIFEST_COLUMNS)[number], unknown>[] => {
  const clips = results.flatMap((r) =>
    r.clips.map((c) => ({ r, c })),
  );
  const rows = clips.map(({ r, c }) => ({
    video_title: r.title ?? r.video.title ?? "",
    video_source: r.video.url ?? r.video.path ?? "",
    clip_rank: c.rank,
    start: c.start,
    end: c.end,
    duration: c.duration,
    hook_text: c.hook_text,
    youtube_title: c.youtube_shorts?.title ?? "",
    tiktok_description: c.tiktok?.description ?? "",
    instagram_caption: c.instagram_reels?.caption ?? "",
    clip_file: path.relative(root, path.join(r.workspace, "output", "clips", c.file)),
    status: r.status,
  }));
  for (const r of results.filter((x) => x.status === "failed")) {
    rows.push({
      video_title: r.title ?? r.video.title ?? "",
      video_source: r.video.url ?? r.video.path ?? "",
      clip_rank: "",
      start: "",
      end: "",
      duration: "",
      hook_text: "",
      youtube_title: "",
      tiktok_description: "",
      instagram_caption: "",
      clip_file: "",
      status: `failed: ${r.error ?? "unknown error"}`,
    } as never);
  }
  return rows;
};

export const toCsv = (rows: Record<string, unknown>[]): string =>
  [MANIFEST_COLUMNS.join(","), ...rows.map((row) => MANIFEST_COLUMNS.map((c) => csvCell(row[c])).join(","))].join("\n") + "\n";

const readClips = (workspace: string): { title: string | null; clips: ClipMeta[] } | null => {
  const file = path.join(workspace, "output", "clips", "metadata.json");
  if (!existsSync(file)) return null;
  const data = JSON.parse(readFileSync(file, "utf8")) as { title?: string | null; clips?: ClipMeta[] };
  return { title: data.title ?? null, clips: data.clips ?? [] };
};

/** Free disk once a video is done: its clips and JSON artifacts stay, the heavy media goes. */
const cleanup = (workspace: string) => {
  for (const p of ["public/source.mp4", "public/broll", "output/audio.wav", "output/work"]) {
    rmSync(path.join(workspace, p), { recursive: true, force: true });
  }
};

const expand = (inputs: string[], limit: number): BatchVideo[] => {
  const res = spawnSync(PYTHON, [path.join(ROOT, "scripts", "expand_urls.py"), "--limit", String(limit), ...inputs], {
    cwd: ROOT,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "inherit"],
    maxBuffer: 64 * 1024 * 1024,
  });
  if (res.status !== 0) throw new Error("Could not expand the batch inputs.");
  return JSON.parse(res.stdout) as BatchVideo[];
};

const main = async () => {
  const sep = process.argv.indexOf("--");
  const own = sep === -1 ? process.argv.slice(2) : process.argv.slice(2, sep);
  const passthrough = sep === -1 ? [] : process.argv.slice(sep + 1);
  const { values } = parseArgs({
    args: own,
    options: {
      urls: { type: "string" },
      url: { type: "string", multiple: true },
      limit: { type: "string", default: "0" },
      clips: { type: "string", default: "3" },
      provider: { type: "string", default: "anthropic" },
      workspaces: { type: "string", default: path.join(ROOT, "workspaces") },
      cleanup: { type: "boolean", default: false },
      force: { type: "boolean", default: false },
      help: { type: "boolean", short: "h", default: false },
    },
  });
  if (values.help || (!values.urls && !values.url?.length)) {
    console.log(`Usage: npx tsx scripts/batch.ts (--urls FILE | --url URL ...) [options] [-- run.ts options]

  --urls FILE        One video/playlist/channel URL or local file per line (# comments)
  --url URL          Repeatable
  --limit N          Max videos per playlist/channel (default: all)
  --clips N          Clips per video (default 3)
  --provider NAME    anthropic | gemini | session | heuristic (default anthropic)
  --workspaces DIR   Where per-video workspaces go (default ./workspaces)
  --cleanup          Delete each video's source/intermediate media after its clips render
  --force            Redo videos that already have clips`);
    process.exit(values.help ? 0 : 1);
  }

  const inputs = [...(values.url ?? [])];
  if (values.urls) {
    inputs.push(
      ...readFileSync(values.urls, "utf8")
        .split(/\r?\n/)
        .map((l) => l.trim())
        .filter((l) => l && !l.startsWith("#")),
    );
  }
  const videos = expand(inputs, Number.parseInt(values.limit ?? "0", 10) || 0);
  const wsRoot = path.resolve(values.workspaces!);
  await mkdir(wsRoot, { recursive: true });
  console.log(`[viral-clipper] Batch: ${videos.length} video(s) → ${path.relative(ROOT, wsRoot) || wsRoot}/`);

  const results: BatchResult[] = [];
  let paused = false;
  for (const [i, video] of videos.entries()) {
    const workspace = path.join(wsRoot, video.id);
    const label = `[${i + 1}/${videos.length}] ${video.title ?? video.url ?? video.path}`;
    if (video.error) {
      console.error(`[viral-clipper] ${label}: failed — ${video.error}`);
      results.push({ video, workspace, status: "failed", error: video.error.split("\n")[0], clips: [] });
      continue;
    }
    const existing = readClips(workspace);
    if (existing?.clips.length && !values.force) {
      console.log(`[viral-clipper] ${label}: already done, skipping (--force to redo).`);
      results.push({ video, workspace, status: "skipped", ...existing });
      continue;
    }

    console.log(`[viral-clipper] ${label}: running pipeline in ${path.relative(ROOT, workspace) || workspace}/`);
    const args = [
      path.join(ROOT, "node_modules", "tsx", "dist", "cli.mjs"),
      path.join(ROOT, "scripts", "run.ts"),
      "--workspace", workspace,
      ...(video.source === "url" ? ["--url", video.url!] : ["--file", video.path!]),
      "--clips", values.clips!,
      "--provider", values.provider!,
      ...(values.force ? ["--force", "--retranscribe"] : []),
      ...passthrough,
    ];
    const res = spawnSync(process.execPath, args, { cwd: ROOT, stdio: "inherit", env: process.env });
    if (res.status === AWAITING_SESSION) {
      // --provider session: this video waits on the Claude Code session. Stop here; re-running the
      // same batch skips finished videos and resumes this one once the request is answered.
      console.log(`[viral-clipper] ${label}: paused for the Claude Code session; re-run the batch after answering.`);
      paused = true;
      break;
    }
    const done = readClips(workspace);
    if (res.status === 0 && done?.clips.length) {
      results.push({ video, workspace, status: "done", ...done });
      if (values.cleanup) cleanup(workspace);
    } else {
      const error = res.status === 0 ? "no clips were rendered" : `pipeline exited with code ${res.status}`;
      console.error(`[viral-clipper] ${label}: failed — ${error}`);
      results.push({ video, workspace, status: "failed", error, clips: done?.clips ?? [] });
    }
  }

  const rows = buildManifestRows(results, ROOT);
  await writeFile(path.join(wsRoot, "batch_manifest.csv"), toCsv(rows));
  await writeFile(
    path.join(wsRoot, "batch_manifest.json"),
    JSON.stringify({ generated_at: new Date().toISOString(), videos: results.map(({ clips, ...r }) => ({ ...r, clips: clips.length })), clips: rows }, null, 2) + "\n",
  );
  const ok = results.filter((r) => r.status !== "failed").length;
  console.log(
    `[viral-clipper] Batch finished: ${ok}/${results.length} video(s) OK, ${rows.filter((r) => r.clip_file).length} clip(s) listed in ` +
      `${path.relative(ROOT, path.join(wsRoot, "batch_manifest.csv"))}`,
  );
  if (paused) process.exitCode = AWAITING_SESSION;
  else if (ok < results.length) process.exitCode = 1;
};

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error(`[viral-clipper] ${err instanceof Error ? err.message : err}`);
    process.exit(1);
  });
}
