import type { CameraKeyframe } from "./schema";

/**
 * Camera x-centre (0-1 of source width) at time t, linearly eased between the
 * tracker's keyframes. A keyframe flagged `cut` is jumped to at its time rather
 * than approached, so speaker switches are hard cuts, not whip-pans.
 */
export const cameraX = (path: CameraKeyframe[], t: number): number => {
  if (!path.length) return 0.5;
  if (t <= path[0].t) return path[0].cx;
  for (let i = 1; i < path.length; i++) {
    const next = path[i];
    if (t < next.t) {
      const prev = path[i - 1];
      if (next.cut) return prev.cx;
      const p = (t - prev.t) / (next.t - prev.t);
      return prev.cx + (next.cx - prev.cx) * p;
    }
  }
  return path[path.length - 1].cx;
};

/**
 * Left offset (px) for a source scaled to fill `frameH`, so that `cx` sits at the
 * centre of a `frameW`-wide window, clamped to keep the window inside the source.
 */
export const cropLeft = (cx: number, sourceW: number, sourceH: number, frameW: number, frameH: number): number => {
  const scaledW = (sourceW * frameH) / sourceH;
  if (scaledW <= frameW) return (frameW - scaledW) / 2;
  return Math.min(0, Math.max(frameW - scaledW, frameW / 2 - cx * scaledW));
};
