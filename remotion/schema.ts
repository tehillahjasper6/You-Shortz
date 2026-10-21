import { z } from "zod";

export const FPS = 30;
export const WIDTH = 1080;
export const HEIGHT = 1920;

export const wordSchema = z.object({
  word: z.string(),
  // Seconds relative to the first frame of the clip (not the source video).
  start: z.number(),
  end: z.number(),
});

export const viralClipSchema = z.object({
  /** Path inside public/, resolved with staticFile(). */
  src: z.string(),
  /** First source frame to show: Math.round(start * fps). */
  trimBefore: z.number().int().min(0),
  /** Clip length in frames: Math.round((end - start) * fps). */
  clipDurationInFrames: z.number().int().min(1),
  words: z.array(wordSchema),
  hookText: z.string(),
  hookDurationInSeconds: z.number().min(0).max(5),
  accentColor: z.string(),
  /** How the source sits over the blurred backdrop. */
  fit: z.enum(["contain", "cover"]),
  /** Dev-only guides showing where TikTok/Reels/Shorts UI covers the frame. */
  showSafeZones: z.boolean(),
});

export type ViralClipProps = z.infer<typeof viralClipSchema>;
export type ClipWord = z.infer<typeof wordSchema>;

export const defaultViralClipProps: ViralClipProps = {
  src: "source.mp4",
  trimBefore: 0,
  clipDurationInFrames: 20 * FPS,
  words: [],
  hookText: "This changes everything",
  hookDurationInSeconds: 2.5,
  accentColor: "#FFE14D",
  fit: "contain",
  showSafeZones: false,
};
