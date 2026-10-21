import assert from "node:assert/strict";
import { test } from "node:test";
import { cameraX, cropLeft } from "../remotion/camera.ts";
import { chooseReframe, clipBroll, type FaceTrack } from "../scripts/04_render.ts";

const path = [
  { t: 0, cx: 0.3 },
  { t: 1, cx: 0.4 },
  { t: 2, cx: 0.8, cut: true },
  { t: 3, cx: 0.8 },
];

test("cameraX eases between keyframes and jumps on cuts", () => {
  assert.equal(cameraX(path, -1), 0.3);
  assert.ok(Math.abs(cameraX(path, 0.5) - 0.35) < 1e-9);
  assert.equal(cameraX(path, 1.9), 0.4, "holds until the cut instead of panning toward it");
  assert.equal(cameraX(path, 2), 0.8);
  assert.equal(cameraX(path, 10), 0.8);
  assert.equal(cameraX([], 1), 0.5);
});

test("cropLeft centres the subject and never shows past the source edges", () => {
  // 1920x1080 scaled to 1920 tall is 3413.3 wide.
  const scaledW = (1920 * 1920) / 1080;
  assert.ok(Math.abs(cropLeft(0.5, 1920, 1080, 1080, 1920) - (540 - 0.5 * scaledW)) < 1e-9);
  assert.equal(cropLeft(0.0, 1920, 1080, 1080, 1920), 0);
  assert.ok(Math.abs(cropLeft(1.0, 1920, 1080, 1080, 1920) - (1080 - scaledW)) < 1e-9);
  assert.equal(cropLeft(0.5, 1080, 1920, 1080, 1920), 0, "vertical sources are just centred");
});

test("chooseReframe tracks only where the tracker found a subject (auto)", () => {
  const tracked: FaceTrack = { start: 0, end: 20, frame_width: 1920, frame_height: 1080, mode: "track", reason: "face tracked", keyframes: path };
  const none: FaceTrack = { ...tracked, mode: "none", reason: "faces in only 10% of samples" };
  assert.equal(chooseReframe("auto", tracked).reframe, "track");
  assert.equal(chooseReframe("auto", none).reframe, "fit");
  assert.equal(chooseReframe("track", none).reframe, "track", "--reframe track forces the path");
  assert.equal(chooseReframe("fit", tracked).reframe, "fit");
  assert.equal(chooseReframe("auto", undefined).reframe, "fit");
});

test("clipBroll re-times cutaways and drops ones that don't fit the clip", () => {
  const base = { duration: 1.5, phrase: "p", query: "q", type: "video" as const, file: "broll/a.mp4", credit: "c" };
  const out = clipBroll([{ ...base, at: 20 }, { ...base, at: 39.5 }, { ...base, at: 5 }], 12.34, 40, 30);
  assert.deepEqual(out, [{ from: +(20 - 370 / 30).toFixed(3), duration: 1.5, src: "broll/a.mp4", type: "video" }]);
});
