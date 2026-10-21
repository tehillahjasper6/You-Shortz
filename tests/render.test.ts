import assert from "node:assert/strict";
import { test } from "node:test";
import { checkHighlight, clipFrames, clipWords, slugify, type Highlight } from "../remotion/clip-props.ts";

test("clipFrames converts absolute seconds to trimBefore + durationInFrames", () => {
  assert.deepEqual(clipFrames(12.34, 37.9, 30), { trimBefore: 370, durationInFrames: 767 });
});

test("clipWords re-times source words relative to trimBefore / fps", () => {
  const words = [
    { word: "before", start: 11.0, end: 11.5 },
    { word: "straddles", start: 12.2, end: 12.5 },
    { word: "inside", start: 13.0, end: 13.4 },
    { word: "tail", start: 37.8, end: 38.2 },
    { word: "after", start: 39.0, end: 39.3 },
  ];
  const out = clipWords(words, 12.34, 37.9, 30);
  assert.deepEqual(
    out.map((w) => w.word),
    ["straddles", "inside", "tail"],
  );
  // trimBefore = 370 frames => offset 12.3333s, not 12.34s.
  assert.equal(out[1].start, +(13.0 - 370 / 30).toFixed(3));
  assert.equal(out[0].start, 0, "words starting before the cut are clamped to frame 0");
  assert.equal(out[2].end, +(37.9 - 370 / 30).toFixed(3), "words running past the cut end with the clip");
});

test("slugify makes filename-safe slugs cut on a word boundary", () => {
  assert.equal(slugify("Why does NOBODY talk about this?!"), "why-does-nobody-talk-about-this");
  assert.equal(slugify("Café au lait — résumé"), "cafe-au-lait-resume");
  assert.equal(slugify("Here is what I wish someone told me earlier today"), "here-is-what-i-wish-someone-told-me");
  assert.equal(slugify("!!!"), "clip");
});

test("checkHighlight rejects broken edits and warns on soft limits", () => {
  const base: Highlight = { rank: 1, start: 10, end: 40, viral_score: 80, hook_text: "Hook", reasoning: "" };
  assert.deepEqual(checkHighlight(base, 100), { errors: [], warnings: [] });
  assert.ok(checkHighlight({ ...base, end: 5 }, 100).errors.length);
  assert.ok(checkHighlight({ ...base, start: -1 }, 100).errors.length);
  assert.ok(checkHighlight({ ...base, end: 120 }, 100).errors.length);
  assert.deepEqual(checkHighlight({ ...base, end: 80 }, 100).errors, []);
  assert.equal(checkHighlight({ ...base, end: 80 }, 100).warnings.length, 1);
});
