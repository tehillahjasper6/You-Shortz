import assert from "node:assert/strict";
import { test } from "node:test";
import { snapEnd, snapStart, viewWindow } from "../review/src/timeline.ts";
import { buildManifestRows, toCsv, type BatchResult } from "../scripts/batch.ts";

const words = [
  { word: "one", start: 10.0, end: 10.4 },
  { word: "two", start: 10.45, end: 10.8 },
  { word: "three", start: 12.0, end: 12.5 },
];

test("snapStart lands 0.3 s before the word, never inside the previous one", () => {
  assert.equal(snapStart(11.0, words), 11.7, "mid-pause drag snaps to the next word with padding");
  assert.equal(snapStart(10.6, words), 10.4, "tight gap: cut at the previous word's end");
  assert.equal(snapStart(9.0, words), 9.7);
});

test("snapEnd lands 0.3 s after the word, never inside the next one", () => {
  assert.equal(snapEnd(11.5, words, 30), 11.1);
  assert.equal(snapEnd(10.6, words, 30), 11.1, "a drag inside a word keeps the whole word");
  assert.equal(snapEnd(10.3, words, 30), 10.45, "tight gap: cut at the next word's start");
  assert.equal(snapEnd(13, words, 12.6), 12.6, "never past the video end");
});

test("viewWindow adds context and clamps to the video", () => {
  assert.deepEqual(viewWindow(5, 30, 32), [0, 32]);
  assert.deepEqual(viewWindow(50, 80, 300, 10), [40, 90]);
});

const clip = (rank: number, score: number, file: string) => ({
  rank,
  file,
  start: 1,
  end: 21,
  duration: 20,
  viral_score: score,
  hook_text: `hook ${rank}, "quoted"`,
  youtube_shorts: { title: `title ${rank}` },
});

test("batch manifest ranks clips across videos and lists failures last", () => {
  const results: BatchResult[] = [
    {
      video: { source: "url", url: "https://y/a", id: "a", title: "A" },
      workspace: "/r/workspaces/a",
      status: "done",
      title: "A",
      clips: [clip(1, 70, "a1.mp4"), clip(2, 90, "a2.mp4")],
    },
    { video: { source: "url", url: "https://y/b", id: "b" }, workspace: "/r/workspaces/b", status: "failed", error: "blocked", clips: [] },
    {
      video: { source: "file", path: "/v/c.mp4", id: "c", title: "C" },
      workspace: "/r/workspaces/c",
      status: "skipped",
      title: "C",
      clips: [clip(1, 80, "c1.mp4")],
    },
  ];
  const rows = buildManifestRows(results, "/r");
  assert.deepEqual(rows.map((r) => r.viral_score), [90, 80, 70, ""]);
  assert.deepEqual(rows.map((r) => r.overall_rank), [1, 2, 3, ""]);
  assert.equal(rows[0].clip_file, "workspaces/a/output/clips/a2.mp4");
  assert.equal(rows[3].status, "failed: blocked");
  const csv = toCsv(rows);
  assert.ok(csv.startsWith("overall_rank,viral_score,"));
  assert.ok(csv.includes('"hook 2, ""quoted"""'), "commas and quotes are CSV-escaped");
});
