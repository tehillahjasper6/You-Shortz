/**
 * Workspace paths for the TypeScript side, mirroring scripts/common.py.
 * A workspace holds one video's public/ and output/; it is the repo root
 * unless $VIRAL_CLIPPER_WORKSPACE points elsewhere (batch mode).
 */
import { cpSync, existsSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const WORKSPACE = path.resolve(process.env.VIRAL_CLIPPER_WORKSPACE || ROOT);
export const OUTPUT = path.join(WORKSPACE, "output");
export const CLIPS_DIR = path.join(OUTPUT, "clips");
export const PUBLIC = path.join(WORKSPACE, "public");

/** Path for log messages: relative to the repo when inside it. */
export const rel = (p: string): string => {
  const r = path.relative(ROOT, p);
  return r.startsWith("..") || path.isAbsolute(r) ? p : r;
};

/** A workspace's public/ is Remotion's static root, so it needs the bundled caption font too. */
export const ensureWorkspacePublic = (): string => {
  const fonts = path.join(PUBLIC, "fonts");
  if (PUBLIC !== path.join(ROOT, "public") && !existsSync(fonts)) {
    mkdirSync(PUBLIC, { recursive: true });
    cpSync(path.join(ROOT, "public", "fonts"), fonts, { recursive: true });
  }
  return PUBLIC;
};
