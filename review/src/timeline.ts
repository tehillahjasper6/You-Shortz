import type { ClipWord } from "../../remotion/schema";

// Same padding rule as 03_detect_viral.py: start 0.2-0.4 s before the first
// word, end 0.2-0.4 s after the last, never inside a neighbouring word.
const PAD = 0.3;
const PAD_MIN = 0.2;
const PAD_MAX = 0.4;

const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

/** Snap a dragged start time to just before the word at (or after) t. */
export const snapStart = (t: number, words: ClipWord[]): number => {
  const i = words.findIndex((w) => w.end > t);
  if (i === -1) return t;
  const anchor = words[i].start;
  const prevEnd = i > 0 ? words[i - 1].end : 0;
  let lo = Math.max(prevEnd, anchor - PAD_MAX, 0);
  let hi = anchor - PAD_MIN;
  if (lo > hi) [lo, hi] = [Math.max(prevEnd, 0), anchor];
  return +clamp(anchor - PAD, lo, hi).toFixed(3);
};

/** Snap a dragged end time to just after the word at (or before) t. */
export const snapEnd = (t: number, words: ClipWord[], duration: number): number => {
  let j = -1;
  for (let k = 0; k < words.length; k++) if (words[k].start < t) j = k;
  if (j === -1) return t;
  const anchor = words[j].end;
  const nextStart = j + 1 < words.length ? words[j + 1].start : duration;
  let lo = anchor + PAD_MIN;
  let hi = Math.min(nextStart, anchor + PAD_MAX, duration);
  if (lo > hi) [lo, hi] = [anchor, Math.min(nextStart, duration)];
  return +clamp(anchor + PAD, lo, hi).toFixed(3);
};

/** The slice of the source the trim timeline shows: the clip plus context on both sides. */
export const viewWindow = (start: number, end: number, duration: number, context = 10): [number, number] => [
  Math.max(0, start - context),
  Math.min(duration || end + context, end + context),
];

export const formatTime = (s: number): string => {
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(2).padStart(5, "0")}`;
};
