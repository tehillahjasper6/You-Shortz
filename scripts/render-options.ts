/**
 * Render flags shared by scripts/04_render.ts and scripts/run.ts. Kept free of
 * workspace-dependent imports so run.ts can parse --workspace before anything
 * resolves workspace paths.
 */
import path from "node:path";
import type { Reframe } from "../remotion/clip-props.ts";

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

