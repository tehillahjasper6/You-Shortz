/**
 * Pure, browser-safe helpers shared by the renderer (scripts/04_render.ts)
 * and the review UI (review/): the artifact types and the mapping from a
 * highlights.json entry to ViralClip props.
 */
import {
  FPS,
  defaultViralClipProps,
  type BrollItem,
  type CameraKeyframe,
  type ClipWord,
  type ViralClipProps,
} from "./schema";

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

export type Highlights = { video_duration?: number; fps?: number; title?: string | null; clips: Highlight[] };
export type Transcript = { duration: number; segments: { words: ClipWord[] }[] };
export type Ingest = { width?: number; height?: number };

export type FaceTrack = {
  start: number;
  end: number;
  frame_width: number;
  frame_height: number;
  mode: "track" | "none";
  reason: string;
  keyframes: CameraKeyframe[];
};
export type FaceTracks = { clips: Record<string, FaceTrack> };

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
export type BrollPlan = { clips: Record<string, { start: number; end: number; items: BrollPlanItem[] }> };

export type Reframe = "auto" | "track" | "fit";

export const MIN_CLIP_S = 15;
export const MAX_CLIP_S = 60;

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
export const matches = (entry: { start: number; end: number } | undefined, h: Highlight) =>
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

/**
 * ViralClip props for one highlight: frame-accurate trim, clip-relative
 * captions, the camera path when a matching face track exists, and B-roll
 * re-timed to the clip. `brollItems` should already be filtered to files
 * that exist.
 */
export const buildClipProps = ({
  clip,
  words,
  ingest,
  track,
  brollItems,
  reframe,
  hookText,
  showSafeZones,
}: {
  clip: Highlight;
  words: ClipWord[];
  ingest: Ingest;
  track?: FaceTrack;
  brollItems: BrollPlanItem[];
  reframe: Reframe;
  hookText?: string;
  showSafeZones?: boolean;
}): { props: ViralClipProps; framing: ReturnType<typeof chooseReframe> } => {
  const { trimBefore, durationInFrames } = clipFrames(clip.start, clip.end);
  const usableTrack = matches(track, clip) ? track : undefined;
  const framing = chooseReframe(reframe, usableTrack);
  return {
    framing,
    props: {
      ...defaultViralClipProps,
      src: "source.mp4",
      trimBefore,
      clipDurationInFrames: durationInFrames,
      words: clipWords(words, clip.start, clip.end),
      hookText: hookText ?? clip.hook_text,
      reframe: framing.reframe,
      sourceWidth: ingest.width ?? usableTrack?.frame_width ?? 0,
      sourceHeight: ingest.height ?? usableTrack?.frame_height ?? 0,
      cameraPath: framing.cameraPath,
      broll: clipBroll(brollItems, clip.start, clip.end),
      showSafeZones: Boolean(showSafeZones),
    },
  };
};
