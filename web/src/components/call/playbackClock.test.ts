/** HQ's speech schedule: back to back while chunks arrive in time, a cushion when the queue ran
 *  dry, and more cushion after underruns mid-utterance — never after a normal pause. */
import { expect, test } from "vitest";

import { PLAYBACK, PlaybackClock } from "./playbackClock";

const CHUNK = 0.04;

test("a fresh utterance starts with the start-up cushion, then plays back to back", () => {
  const c = new PlaybackClock();
  expect(c.schedule(10, CHUNK)).toBeCloseTo(10 + PLAYBACK.startS);
  expect(c.schedule(10.04, CHUNK)).toBeCloseTo(10 + PLAYBACK.startS + CHUNK);
  expect(c.schedule(10.08, CHUNK)).toBeCloseTo(10 + PLAYBACK.startS + 2 * CHUNK);
});

test("chunks arriving late by less than the cushion leave no gap at all", () => {
  // The cushion is what jitter spends: a chunk may arrive up to the cushion late and still be
  // in time, because the queue is still playing when it lands.
  const c = new PlaybackClock();
  const jitter = [0, 0.08, 0.02, 0.08, 0, 0.08, 0.05, 0.07];
  let prevEnd = -1;
  for (let i = 0; i < jitter.length; i++) {
    const at = c.schedule(10 + i * CHUNK + jitter[i], CHUNK);
    if (prevEnd >= 0) expect(at).toBeCloseTo(prevEnd); // contiguous
    prevEnd = at + CHUNK;
  }
  expect(c.underruns).toBe(0);
});

test("an underrun mid-utterance grows the cushion, up to the maximum", () => {
  const c = new PlaybackClock();
  c.schedule(10, CHUNK); // plays until 10.16
  c.schedule(10.3, CHUNK); // arrived after the queue ran dry, 140 ms ago: an underrun
  expect(c.underruns).toBe(1);
  expect(c.cushion).toBeCloseTo(PLAYBACK.startS + PLAYBACK.stepS);
  // Arrivals 0.45 s apart: each finds the queue dry (an underrun, within the pause window).
  for (let i = 0; i < 20; i++) c.schedule(10.3 + (i + 1) * 0.45, CHUNK);
  expect(c.cushion).toBeCloseTo(PLAYBACK.maxS);
});

test("a pause between utterances is not an underrun", () => {
  const c = new PlaybackClock();
  c.schedule(10, CHUNK);
  const at = c.schedule(12, CHUNK); // two seconds later: a new utterance
  expect(c.underruns).toBe(0);
  expect(at).toBeCloseTo(12 + PLAYBACK.startS);
});

test("a flush (barge-in) empties the queue; the next speech starts afresh with the cushion", () => {
  const c = new PlaybackClock();
  c.schedule(10, CHUNK);
  c.schedule(10.04, CHUNK);
  c.flush();
  expect(c.end).toBe(0);
  expect(c.schedule(10.05, CHUNK)).toBeCloseTo(10.05 + PLAYBACK.startS);
  expect(c.underruns).toBe(0);
});

test("a chunk arriving while the queue still plays is contiguous — never an underrun (Hermes 6984)", () => {
  // First chunk at 10 s plays 10.12–10.16; chunks arriving 1–29 ms before 10.16 go straight on.
  for (const early of [0.001, 0.01, 0.02, 0.029]) {
    const c = new PlaybackClock();
    c.schedule(10, CHUNK);
    const at = c.schedule(10.16 - early, CHUNK);
    expect(at).toBeCloseTo(10 + PLAYBACK.startS + CHUNK);
    expect(c.underruns).toBe(0);
    expect(c.cushion).toBeCloseTo(PLAYBACK.startS);
  }
});

test("each new utterance, and a flush, starts again from the start-up cushion (Hermes 6984)", () => {
  const c = new PlaybackClock();
  c.schedule(10, CHUNK); // plays until 10.16
  c.schedule(10.31, CHUNK); // a real underrun: the cushion grows for THIS utterance
  expect(c.cushion).toBeCloseTo(PLAYBACK.startS + PLAYBACK.stepS);
  expect(c.schedule(12, CHUNK)).toBeCloseTo(12 + PLAYBACK.startS); // a new utterance: fresh
  c.schedule(12.4, CHUNK); // another underrun mid-utterance
  c.flush(); // barge-in
  expect(c.schedule(13, CHUNK)).toBeCloseTo(13 + PLAYBACK.startS);
});
