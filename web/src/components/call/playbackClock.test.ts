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
  c.schedule(10, CHUNK);
  c.schedule(c.end + 0.14, CHUNK); // arrived after the queue ran dry, 140 ms ago: an underrun
  expect(c.underruns).toBe(1);
  expect(c.cushion).toBeCloseTo(PLAYBACK.startS + PLAYBACK.stepS);
  // Each arrival finds the queue dry (an underrun, within the pause window).
  for (let i = 0; i < 10; i++) c.schedule(c.end + 0.14, CHUNK);
  expect(c.cushion).toBeCloseTo(PLAYBACK.maxS);
});

test("a pause between utterances is not an underrun", () => {
  const c = new PlaybackClock();
  c.schedule(10, CHUNK);
  const next = c.end + PLAYBACK.pauseS + 0.1; // dry for longer than a reply's hole: a new utterance
  const at = c.schedule(next, CHUNK);
  expect(c.underruns).toBe(0);
  expect(at).toBeCloseTo(next + PLAYBACK.startS);
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
  // The first chunk plays from 10 s + the cushion; chunks arriving 1–29 ms before it ends go
  // straight on.
  for (const early of [0.001, 0.01, 0.02, 0.029]) {
    const c = new PlaybackClock();
    c.schedule(10, CHUNK);
    const at = c.schedule(10 + PLAYBACK.startS + CHUNK - early, CHUNK);
    expect(at).toBeCloseTo(10 + PLAYBACK.startS + CHUNK);
    expect(c.underruns).toBe(0);
    expect(c.cushion).toBeCloseTo(PLAYBACK.startS);
  }
});

test("each new utterance, and a flush, starts again from the start-up cushion (Hermes 6984)", () => {
  const c = new PlaybackClock();
  c.schedule(10, CHUNK);
  c.schedule(c.end + 0.15, CHUNK); // a real underrun: the cushion grows for THIS utterance
  expect(c.cushion).toBeCloseTo(PLAYBACK.startS + PLAYBACK.stepS);
  const fresh = c.end + PLAYBACK.pauseS + 0.5;
  expect(c.schedule(fresh, CHUNK)).toBeCloseTo(fresh + PLAYBACK.startS); // a new utterance: fresh
  c.schedule(c.end + 0.15, CHUNK); // another underrun mid-utterance
  c.flush(); // barge-in
  expect(c.schedule(fresh + 10, CHUNK)).toBeCloseTo(fresh + 10 + PLAYBACK.startS);
});

test("speech synthesized slower than real time rebuffers once or twice, not at every chunk", () => {
  // A self-hosted TTS measured at up to 2x slower than real time: 4 s of speech whose 40 ms
  // chunks arrive at 0.6x. The 60 ms / 0.4 s cushion ran dry 8 times on this (stutter); the
  // rebuffer spends the same shortfall as 2 pauses.
  const c = new PlaybackClock();
  for (let i = 0; i < 100; i++) c.schedule(10 + (i * CHUNK) / 0.6, CHUNK);
  expect(c.underruns).toBeLessThanOrEqual(2);
});

test("a hole shorter than the pause window, inside one reply, keeps the grown cushion", () => {
  // A slow sentence can leave the queue dry for a second or more between chunks of one reply.
  const c = new PlaybackClock();
  c.schedule(10, CHUNK);
  c.schedule(c.end + 0.14, CHUNK); // an underrun: the cushion grows
  const grown = c.cushion;
  c.schedule(c.end + 1.2, CHUNK); // a 1.2 s hole in the same reply: still mid-utterance
  expect(c.cushion).toBeGreaterThanOrEqual(grown);
});
