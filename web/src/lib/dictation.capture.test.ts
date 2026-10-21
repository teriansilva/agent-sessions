import { expect, test } from "vitest";
import { assembleSpoken, type SpokenSegment } from "./dictation";

// A REAL device capture, replayed verbatim: Android 10 / Chrome 150, `continuous: true` (no
// single-utterance fallback), taken from /dictation-probe.html on the device that reported #749.
//
// Its shape is the whole point. Every entry but the last is born final; three of them are finalized
// EMPTY strings (the #711 stacker fingerprint); each event appends exactly one cumulative snapshot;
// and the LAST entry is the live phrase, which arrives as an interim at t=4125 and keeps growing
// until t=4313 as the speaker finishes the sentence.
//
// On v0.15.0 that last entry could not absorb its predecessor — it was not born final, and its
// `atMs` (latest revision, 4313) put it 514 ms past the one before it, outside SNAPSHOT_BURST_MS.
// Measured from ARRIVAL it is 326 ms, comfortably in-burst. So the sentence was typed twice:
//   "and when you are done with and when you are done with all of this"
// This test is that stream, and it must assemble to the sentence exactly once.
const EV: [number, [number, boolean, string][]][] = [
  [1949, [[0, true, ""]]],
  [2248, [[1, true, ""]]],
  [2457, [[2, true, ""]]],
  [2471, [[3, true, "and"]]],
  [2556, [[4, true, "and"]]],
  [2657, [[5, true, "and"]]],
  [2765, [[6, true, "and when"]]],
  [2969, [[7, true, "and when"]]],
  [3074, [[8, true, "and when you"]]],
  [3246, [[9, true, "and when you are"]]],
  [3286, [[10, true, "and when you are done"]]],
  [3378, [[11, true, "and when you are done"]]],
  [3482, [[12, true, "and when you are done with"]]],
  [3688, [[13, true, "and when you are done with"]]],
  [3799, [[14, true, "and when you are done with"]]],
  [4125, [[15, false, "and when you are done with all"]]],
  [4307, [[15, false, "and when you are done with all of"]]],
  [4313, [[15, false, "and when you are done with all of this"]]],
  [4319, [[15, true, "and when you are done with all of this"]]],
];

test("the captured device stream assembles to the sentence ONCE (#749)", () => {
  const text: string[] = [],
    at: number[] = [],
    firstAt: number[] = [],
    evq: number[] = [],
    born: boolean[] = [];
  const live = new Map<number, { isFinal: boolean; tr: string }>();
  let seq = 0;
  for (const [t, upd] of EV) {
    seq++;
    for (const [i, isFinal, tr] of upd) live.set(i, { isFinal, tr });
    for (const [i, e] of [...live].sort((a, b) => a[0] - b[0])) {
      if (text[i] === undefined) {
        born[i] = e.isFinal;
        firstAt[i] = t;
      }
      if (text[i] !== e.tr) {
        text[i] = e.tr;
        at[i] = t;
        evq[i] = seq;
      }
    }
  }
  const n = live.size;
  const segs: SpokenSegment[] = [];
  for (let i = 0; i < n; i++) {
    const e = live.get(i)!;
    if (e.isFinal || i === n - 1)
      segs.push({
        text: text[i],
        atMs: at[i],
        firstAtMs: firstAt[i],
        eventSeq: evq[i],
        isFinal: e.isFinal,
        finalBorn: born[i],
      });
  }
  // The two legs that fail on the FINAL pair:
  expect(at[15] - at[14]).toBe(514); // latest-revision gap — what the old rule saw
  expect(firstAt[15] - at[14]).toBe(326); // ARRIVAL gap — comfortably in-burst
  expect(born[15]).toBe(false); // …and the live entry is never "born final"
  expect(assembleSpoken(segs, true)).toBe(
    "and when you are done with all of this",
  );
});
