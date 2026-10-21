/**
 * The review UI's backend, as a Vite dev-server plugin:
 *
 *   GET  /api/state        everything the storyboard needs from the workspace
 *   PUT  /api/highlights   save edited clips (validated) back to highlights.json
 *   POST /api/render       render chosen ranks via scripts/run.ts (one job at a time)
 *   GET  /api/render       job status + log tail
 *   GET  /clips/<file>     finished renders from output/clips/
 */
import { spawn } from "node:child_process";
import { createReadStream, existsSync, readFileSync, statSync } from "node:fs";
import { rename, writeFile } from "node:fs/promises";
import type { IncomingMessage, ServerResponse } from "node:http";
import path from "node:path";
import type { Plugin } from "vite";
import { checkHighlight, type Highlight, type Highlights } from "../remotion/clip-props.ts";
import { CLIPS_DIR, OUTPUT, PUBLIC, ROOT, WORKSPACE } from "../scripts/paths.ts";

const file = (name: string) => path.join(OUTPUT, name);
const readJson = <T,>(p: string, fallback: T): T => (existsSync(p) ? (JSON.parse(readFileSync(p, "utf8")) as T) : fallback);

type Job = { running: boolean; ranks: number[]; log: string[]; exitCode: number | null; startedAt: string };
let job: Job | null = null;
const LOG_LINES = 400;

const send = (res: ServerResponse, status: number, body: unknown) => {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(body));
};

const body = async (req: IncomingMessage): Promise<unknown> => {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(c as Buffer);
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
};

const state = () => {
  const highlights = readJson<Highlights | null>(file("highlights.json"), null);
  const transcript = readJson<{ duration: number; segments: { text: string; start: number; end: number; words: unknown[] }[] }>(
    file("transcript.json"),
    { duration: 0, segments: [] },
  );
  const broll = readJson<{ clips: Record<string, { start: number; end: number; items: { file: string }[] }> }>(file("broll.json"), { clips: {} });
  // Only offer cutaways whose media is on disk, as the renderer does.
  for (const c of Object.values(broll.clips)) c.items = c.items.filter((it) => existsSync(path.join(PUBLIC, it.file)));
  return {
    workspace: path.relative(ROOT, WORKSPACE) || ".",
    hasSource: existsSync(path.join(PUBLIC, "source.mp4")),
    ingest: readJson(file("ingest.json"), {}),
    highlights,
    duration: highlights?.video_duration ?? transcript.duration,
    words: transcript.segments.flatMap((s) => s.words),
    segments: transcript.segments.map(({ text, start, end }) => ({ text, start, end })),
    faceTracks: readJson(file("face_tracks.json"), { clips: {} }).clips,
    broll: broll.clips,
    rendered: readJson(path.join(CLIPS_DIR, "metadata.json"), { clips: [] }).clips,
    job,
  };
};

const saveHighlights = async (clips: Highlight[]) => {
  const current = readJson<Highlights | null>(file("highlights.json"), null);
  if (!current) throw new Error("output/highlights.json does not exist yet");
  const duration = current.video_duration ?? 0;
  const problems = clips.flatMap((c) => checkHighlight(c, duration).errors.map((e) => `clip #${c.rank}: ${e}`));
  if (problems.length) throw new Error(problems.join("; "));
  const next = {
    ...current,
    clips: clips.map((c) => ({ ...c, duration: +(c.end - c.start).toFixed(3), hook_text: c.hook_text.trim() })),
    edited_at: new Date().toISOString(),
  };
  const tmp = file("highlights.json.tmp");
  await writeFile(tmp, JSON.stringify(next, null, 2) + "\n");
  await rename(tmp, file("highlights.json"));
};

const startRender = (ranks: number[], extraArgs: string[]) => {
  const args = [
    path.join(ROOT, "node_modules", "tsx", "dist", "cli.mjs"),
    path.join(ROOT, "scripts", "run.ts"),
    "--workspace", WORKSPACE,
    "--only", ranks.join(","),
    ...extraArgs,
  ];
  const current: Job = { running: true, ranks, log: [], exitCode: null, startedAt: new Date().toISOString() };
  job = current;
  const child = spawn(process.execPath, args, { cwd: ROOT, env: process.env });
  const onData = (chunk: Buffer) => {
    for (const line of chunk.toString("utf8").split(/\r?\n/)) {
      if (!line.trim() || /memory|docker/i.test(line)) continue;
      current.log.push(line);
    }
    if (current.log.length > LOG_LINES) current.log.splice(0, current.log.length - LOG_LINES);
  };
  child.stdout.on("data", onData);
  child.stderr.on("data", onData);
  child.on("close", (code) => {
    current.running = false;
    current.exitCode = code;
  });
};

/** Render options the UI may pass through to run.ts; anything else is rejected. */
const renderArgs = (opts: Record<string, unknown>): string[] => {
  const args: string[] = [];
  if (opts.variants) args.push("--variants");
  if (opts.safeZones) args.push("--safe-zones");
  if (opts.noBroll) args.push("--no-broll");
  if (typeof opts.reframe === "string" && ["auto", "track", "fit"].includes(opts.reframe)) args.push("--reframe", opts.reframe);
  return args;
};

export const reviewApi = (): Plugin => ({
  name: "viral-clipper-review-api",
  configureServer(server) {
    server.middlewares.use(async (req, res, next) => {
      const url = new URL(req.url ?? "/", "http://local");
      try {
        if (url.pathname === "/api/state" && req.method === "GET") return send(res, 200, state());
        if (url.pathname === "/api/highlights" && req.method === "PUT") {
          const { clips } = (await body(req)) as { clips: Highlight[] };
          await saveHighlights(clips);
          return send(res, 200, state());
        }
        if (url.pathname === "/api/render" && req.method === "GET") return send(res, 200, { job });
        if (url.pathname === "/api/render" && req.method === "POST") {
          if (job?.running) return send(res, 409, { error: "A render is already running." });
          const { ranks, options } = (await body(req)) as { ranks: number[]; options?: Record<string, unknown> };
          const valid = (ranks ?? []).filter((r) => Number.isInteger(r) && r > 0);
          if (!valid.length) return send(res, 400, { error: "Choose at least one clip to render." });
          startRender(valid, renderArgs(options ?? {}));
          return send(res, 202, { job });
        }
        if (url.pathname.startsWith("/clips/") && req.method === "GET") {
          const name = path.basename(decodeURIComponent(url.pathname.slice("/clips/".length)));
          const p = path.join(CLIPS_DIR, name);
          if (!name.endsWith(".mp4") || !existsSync(p)) return send(res, 404, { error: "not found" });
          res.setHeader("Content-Type", "video/mp4");
          res.setHeader("Content-Length", statSync(p).size);
          return createReadStream(p).pipe(res);
        }
      } catch (err) {
        return send(res, 400, { error: err instanceof Error ? err.message : String(err) });
      }
      next();
    });
  },
});
