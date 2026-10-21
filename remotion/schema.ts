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

export const cameraKeyframeSchema = z.object({
  /** Seconds from the first frame of the clip. */
  t: z.number(),
  /** Horizontal centre of the crop window, 0-1 of the source width. */
  cx: z.number(),
  /** Jump to this position at `t` instead of easing from the previous keyframe. */
  cut: z.boolean().optional(),
});

export const brollItemSchema = z.object({
  /** Seconds from the first frame of the clip. */
  from: z.number(),
  duration: z.number(),
  /** Path inside public/. */
  src: z.string(),
  type: z.enum(["video", "image"]),
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
  /** "fit": source over a blurred backdrop; "track": a 9:16 window that follows the camera path. */
  reframe: z.enum(["fit", "track"]),
  /** How the source sits over the blurred backdrop in "fit" mode. */
  fit: z.enum(["contain", "cover"]),
  sourceWidth: z.number().int().min(0),
  sourceHeight: z.number().int().min(0),
  cameraPath: z.array(cameraKeyframeSchema),
  broll: z.array(brollItemSchema),
  /** Dev-only guides showing where TikTok/Reels/Shorts UI covers the frame. */
  showSafeZones: z.boolean(),
});

export type ViralClipProps = z.infer<typeof viralClipSchema>;
export type ClipWord = z.infer<typeof wordSchema>;
export type CameraKeyframe = z.infer<typeof cameraKeyframeSchema>;
export type BrollItem = z.infer<typeof brollItemSchema>;

export const defaultViralClipProps: ViralClipProps = {
  src: "source.mp4",
  trimBefore: 0,
  clipDurationInFrames: 20 * FPS,
  words: [],
  hookText: "This changes everything",
  hookDurationInSeconds: 2.5,
  accentColor: "#FFE14D",
  reframe: "fit",
  fit: "contain",
  sourceWidth: 1920,
  sourceHeight: 1080,
  cameraPath: [],
  broll: [],
  showSafeZones: false,
};
