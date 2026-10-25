/** #1667 — the call's noise gate: near-silence sends silence, everything louder gets through
 *  whole, and nothing a room does can make it cut a voice. */
import { describe, expect, test } from "vitest";

import { GATE, NoiseGate, frameDb } from "./noiseGate";
import { FRAME_SAMPLES, Framer, floatTo16, IN_RATE } from "./pcm";

/** A deterministic noise source (so a failing run reproduces). */
function rng(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 0x100000000;
  };
}
const amp = (db: number) => Math.pow(10, db / 20);

/** One 20 ms 16 kHz PCM16 frame of noise at about `db` dBFS RMS. */
function noise(db: number, r = rng(7)): ArrayBuffer {
  const f = new Float32Array(FRAME_SAMPLES);
  const a = amp(db) * Math.sqrt(3); // uniform in [-a, a] has RMS a/√3
  for (let i = 0; i < f.length; i++) f[i] = (r() * 2 - 1) * a;
  return floatTo16(f).buffer as ArrayBuffer;
}
/** A voiced frame: a 220 Hz tone at about `db` dBFS RMS. */
function voice(db: number, t0 = 0): ArrayBuffer {
  const f = new Float32Array(FRAME_SAMPLES);
  const a = amp(db) * Math.SQRT2;
  for (let i = 0; i < f.length; i++) f[i] = a * Math.sin((2 * Math.PI * 220 * (t0 + i)) / IN_RATE);
  return floatTo16(f).buffer as ArrayBuffer;
}
/** A normal deviate (Box–Muller) from `r`. */
function gauss(r: () => number): number {
  return Math.sqrt(-2 * Math.log(1 - r())) * Math.cos(2 * Math.PI * r());
}
const zeros = (n: number) => Array.from({ length: n }, () => new ArrayBuffer(640));
const isSilence = (b: ArrayBuffer) => new Int16Array(b).every((s) => s === 0);
/** A quiet room after the browser's noise suppression: near-silence, under the gate. */
const ROOM = -65;

/** Run frames through a gate; what went out, in order. */
function run(gate: NoiseGate, frames: ArrayBuffer[]): ArrayBuffer[] {
  return frames.flatMap((f) => gate.push(f));
}
/** How many of `frames` went out as themselves (not as silence). */
const heard = (out: ArrayBuffer[], frames: ArrayBuffer[]) => out.filter((f) => frames.includes(f)).length;

describe("frameDb", () => {
  test("measures dBFS and calls digital silence -120", () => {
    expect(frameDb(new ArrayBuffer(640))).toBe(-120);
    expect(frameDb(voice(-20))).toBeCloseTo(-20, 0);
    expect(frameDb(noise(-50))).toBeCloseTo(-50, 0);
  });
});

describe("NoiseGate", () => {
  test("a near-silent room sends only silence, one frame per frame after the pre-roll fills", () => {
    const r = rng(1);
    const g = new NoiseGate();
    const frames = Array.from({ length: 300 }, () => noise(ROOM, r)); // 6 s
    const out = run(g, frames);
    expect(out).toHaveLength(frames.length - GATE.preRollFrames);
    expect(out.every(isSilence)).toBe(true);
    expect(g.open).toBe(false);
  });

  test("speech opens it at once, and the 200 ms before the onset goes out as real audio", () => {
    const r = rng(2);
    const g = new NoiseGate();
    const room = Array.from({ length: 100 }, () => noise(ROOM, r));
    const before = run(g, room);
    const word = [0, 1, 2].map((k) => voice(-22, k * FRAME_SAMPLES));
    // The first two loud frames wait in the queue for the vote (a click is one or two frames);
    // only the oldest queued room frames leave, as silence.
    const held = [...g.push(word[0]), ...g.push(word[1])];
    expect(held).toHaveLength(2);
    expect(held.every(isSilence)).toBe(true);
    const out = g.push(word[2]);
    expect(g.open).toBe(true);
    // The queue goes out as the real audio: the room just before the word, then the word.
    expect(out).toEqual([...room.slice(-(GATE.preRollFrames - 2)), ...word]);
    expect(before.length + held.length + out.length).toBe(room.length + 3);
  });

  test("every captured frame is sent exactly once, through any number of openings", () => {
    const r = rng(3);
    const g = new NoiseGate();
    let captured = 0;
    let sent = 0;
    for (let round = 0; round < 5; round++) {
      for (let i = 0; i < 60; i++, captured++) sent += g.push(noise(ROOM, r)).length;
      for (let i = 0; i < 40; i++, captured++) sent += g.push(voice(-24, i * FRAME_SAMPLES)).length;
      for (let i = 0; i < 30; i++, captured++) sent += g.push(new ArrayBuffer(640)).length;
    }
    for (let i = 0; i < GATE.holdFrames + 2 * GATE.preRollFrames + 20; i++, captured++)
      sent += g.push(noise(ROOM, r)).length;
    // While closed, the pre-roll still waits in the queue: that is the only difference.
    expect(sent).toBe(captured - GATE.preRollFrames);
  });

  test("the hold bridges a pause between words; a long pause closes it", () => {
    const r = rng(4);
    const g = new NoiseGate();
    run(g, Array.from({ length: 100 }, () => noise(ROOM, r)));
    run(g, Array.from({ length: 20 }, (_, i) => voice(-24, i * FRAME_SAMPLES)));
    const pause = Array.from({ length: 40 }, () => noise(ROOM, r)); // 0.8 s, thinking
    expect(heard(run(g, pause), pause)).toBe(40); // the pause itself is real audio
    expect(g.open).toBe(true);
    run(g, Array.from({ length: GATE.holdFrames + 5 }, () => noise(ROOM, r)));
    expect(g.open).toBe(false);
  });

  test("talking for a long time, with breaths between phrases, is heard whole", () => {
    const r = rng(12);
    const g = new NoiseGate();
    run(g, Array.from({ length: 100 }, () => noise(ROOM, r)));
    const talk: ArrayBuffer[] = [];
    for (let phrase = 0; phrase < 40; phrase++) {
      for (let i = 0; i < 25; i++) talk.push(voice(-30 - (phrase % 9), i * FRAME_SAMPLES));
      for (let i = 0; i < 5; i++) talk.push(noise(ROOM, r)); // a breath
    }
    expect(heard(run(g, talk), talk)).toBe(talk.length);
    expect(g.open).toBe(true);
  });

  test("a voice dipping under the level for less than the hold is heard whole", () => {
    // A soft word trailing off, or a dip mid-word: up to 1 s under -55 dBFS stays real audio.
    const r = rng(13);
    const g = new NoiseGate();
    run(g, Array.from({ length: 100 }, () => noise(ROOM, r)));
    const words = [
      ...Array.from({ length: 30 }, (_, i) => voice(-20, i * FRAME_SAMPLES)),
      ...Array.from({ length: 40 }, (_, i) => voice(-58, i * FRAME_SAMPLES)), // trailing off
      ...Array.from({ length: 30 }, (_, i) => voice(-30, i * FRAME_SAMPLES)),
    ];
    expect(heard(run(g, words), words)).toBe(words.length);
  });

  test("a voice whose frames straddle the level opens it by their average (#1669 re-review)", () => {
    const r = rng(18);
    const g = new NoiseGate();
    run(g, Array.from({ length: 100 }, () => noise(ROOM, r)));
    const said = Array.from({ length: 100 }, (_, i) => voice(i % 2 ? -57 : -53, i * FRAME_SAMPLES));
    expect(heard(run(g, said), said)).toBe(100);
  });

  test("a room just under the level never latches it open after speech (#1669 re-review)", () => {
    // A separate, lower closing level kept a -57…-60 dBFS room open forever after one word.
    const rooms: [string, (r: () => number) => number][] = [
      ["steady -57", () => -57],
      ["steady -58", () => -58],
      ["steady -59.5", () => -59.5],
      ["-62 ±2", (r) => -62 + 2 * gauss(r)],
      ["-59.5 ±1.5", (r) => -59.5 + 1.5 * gauss(r)],
    ];
    for (const [name, level] of rooms) {
      for (let seed = 0; seed < 10; seed++) {
        const r = rng(200 + seed);
        const g = new NoiseGate();
        run(g, Array.from({ length: 50 }, () => noise(level(r), r)));
        run(g, Array.from({ length: 50 }, (_, i) => voice(-30, i * FRAME_SAMPLES))); // 1 s word
        const after = run(g, Array.from({ length: 1500 }, () => noise(level(r), r)));
        expect(g.open, `${name}, seed ${seed}`).toBe(false);
        expect(after.slice(-1400).every(isSilence), `${name}, seed ${seed}`).toBe(true);
      }
    }
  });

  test("a jittery room under the level never opens it", () => {
    const r = rng(19);
    const g = new NoiseGate();
    const out = run(g, Array.from({ length: 3000 }, () => noise(-60 + 2 * gauss(r), r)));
    expect(out.every(isSilence)).toBe(true);
  });

  test("a click (one loud frame) doesn't open it; a word, two frames in, does, onset intact", () => {
    const r = rng(14);
    const g = new NoiseGate();
    run(g, Array.from({ length: 50 }, () => noise(ROOM, r)));
    const click = voice(-10);
    run(g, [click, ...Array.from({ length: 30 }, () => noise(ROOM, r))]);
    expect(g.open).toBe(false);
    const word = [voice(-30), voice(-30, FRAME_SAMPLES), voice(-30, 2 * FRAME_SAMPLES)];
    expect(heard(run(g, word), word)).toBe(3);
    expect(g.open).toBe(true);
  });

  test("a quiet, even voice in a room of digital zeros is heard, every utterance (#1669 review)", () => {
    // Noise suppression can send exact zeros between words. The old floor rules never heard a
    // voice at -42 ±3 dBFS here (0/100 per utterance).
    const r = rng(15);
    for (const [db, spread] of [
      [-42, 3],
      [-40, 2],
      [-50, 4],
    ]) {
      const g = new NoiseGate();
      run(g, zeros(100));
      for (let u = 0; u < 4; u++) {
        const said = Array.from({ length: 100 }, (_, i) => voice(db + spread * (2 * r() - 1), i * FRAME_SAMPLES));
        expect(heard(run(g, said), said)).toBe(100);
        run(g, zeros(80));
      }
    }
  });

  test("talking while unmuting (a fresh gate) loses nothing of the first word", () => {
    const r = rng(16);
    const g = new NoiseGate();
    g.reset();
    const said = Array.from({ length: 150 }, (_, i) => voice(-48 + 5 * (2 * r() - 1), i * FRAME_SAMPLES));
    expect(heard(run(g, said), said)).toBe(150);
  });

  test("a prompt answer right after digital silence (a fresh call, an unmute) gets through", () => {
    const g = new NoiseGate();
    const answer = Array.from({ length: 15 }, (_, i) => voice(-25, i * FRAME_SAMPLES));
    const out = run(g, [...zeros(25), ...answer, ...zeros(60)]);
    expect(heard(out, answer)).toBe(15);
  });

  test("nothing a quiet room does opens it: dips, dropouts, warm-up zeros, jitter (#1669 review)", () => {
    const r = rng(17);
    const g = new NoiseGate();
    const frames = [
      ...zeros(25), // a mic warming up
      ...Array.from({ length: 150 }, () => noise(ROOM, r)),
      ...Array.from({ length: 150 }, () => noise(-80, r)), // the echo canceller ducking it
      ...Array.from({ length: 150 }, () => noise(ROOM, r)),
      ...Array.from({ length: 50 }, () => {
        const f = new Int16Array(noise(ROOM, r));
        f.fill(0, 10, 310); // a dropout from a starved audio thread
        return f.buffer as ArrayBuffer;
      }),
      ...Array.from({ length: 300 }, () => noise(ROOM + 3 * gauss(r), r)), // jitter, σ = 3 dB
    ];
    expect(run(g, frames).every(isSilence)).toBe(true);
    expect(g.open).toBe(false);
  });

  test("after speech, a jittery quiet room always closes it again (#1669 review)", () => {
    for (let seed = 0; seed < 20; seed++) {
      const r = rng(100 + seed);
      const g = new NoiseGate();
      run(g, Array.from({ length: 150 }, () => noise(ROOM, r)));
      run(g, Array.from({ length: 20 }, (_, i) => voice(-24, i * FRAME_SAMPLES)));
      const after = run(g, Array.from({ length: 1500 }, () => noise(ROOM + 2 * gauss(r), r)));
      expect(g.open, `seed ${seed}`).toBe(false);
      expect(after.slice(-100).every(isSilence)).toBe(true);
    }
  });

  test("clicks never open it: two close together, one split over two frames, one in a quiet room (#1669 review)", () => {
    const click = () => {
      const f = new Int16Array(640 / 2);
      for (let i = 0; i < 32; i++) f[i] = (i % 2 ? -1 : 1) * 20000; // a 2 ms click
      return f.buffer as ArrayBuffer;
    };
    const split = () => {
      const a = new Int16Array(640 / 2);
      const b = new Int16Array(640 / 2);
      for (let i = 0; i < 24; i++) a[a.length - 24 + i] = (i % 2 ? -1 : 1) * 20000;
      for (let i = 0; i < 24; i++) b[i] = (i % 2 ? -1 : 1) * 20000;
      return [a.buffer as ArrayBuffer, b.buffer as ArrayBuffer];
    };
    for (let gap = 0; gap <= 3; gap++) {
      const r = rng(30 + gap);
      const g = new NoiseGate();
      run(g, Array.from({ length: 50 }, () => noise(ROOM, r)));
      const out = run(g, [click(), ...Array.from({ length: gap }, () => noise(ROOM, r)), click(),
        ...Array.from({ length: 60 }, () => noise(ROOM, r))]); // fmt: skip
      expect(out.every(isSilence), `two clicks ${gap} frames apart`).toBe(true);
    }
    const g = new NoiseGate();
    const r = rng(40);
    run(g, Array.from({ length: 50 }, () => noise(ROOM, r)));
    expect(run(g, [...split(), ...Array.from({ length: 60 }, () => noise(ROOM, r))]).every(isSilence)).toBe(true);
    for (let seed = 0; seed < 200; seed++) {
      const q = rng(500 + seed);
      const h = new NoiseGate();
      const level = () => -62 + 2 * gauss(q);
      run(h, Array.from({ length: 50 }, () => noise(level(), q)));
      const out = run(h, [click(), ...Array.from({ length: 60 }, () => noise(level(), q))]);
      expect(out.every(isSilence), `a click in a -62 ±2 room, seed ${seed}`).toBe(true);
    }
  });

  test("a short word right after digital silence, and speech full of zero frames, are heard (#1669 review)", () => {
    for (const db of [-20, -40, -45, -54]) {
      const g = new NoiseGate();
      run(g, zeros(50));
      const word = [0, 1, 2, 3].map((k) => voice(db, k * FRAME_SAMPLES)); // 80 ms
      expect(heard(run(g, [...word, ...zeros(60)]), word), `${db} dBFS after zeros`).toBe(4);
    }
    // Hard noise suppression: 3 frames of voice, then 2 (or 1) frames of exact zeros, over and over.
    for (const [db, gapFrames] of [
      [-30, 2],
      [-40, 1],
    ]) {
      const g = new NoiseGate();
      run(g, zeros(50));
      const speech: ArrayBuffer[] = [];
      const frames: ArrayBuffer[] = [];
      for (let k = 0; k < 40; k++) {
        const bit = [0, 1, 2].map((i) => voice(db, i * FRAME_SAMPLES));
        speech.push(...bit);
        frames.push(...bit, ...zeros(gapFrames));
      }
      expect(heard(run(g, frames), speech), `${db} dBFS with ${gapFrames}-frame gaps`).toBe(speech.length);
    }
  });

  test("a sound loud enough passes whole while it lasts, a steady one too, then it closes", () => {
    // Whether a steady sound is a machine or a voice is the worker's Silero VAD's call: the
    // gate passes it, and closes once the room is near-silent again.
    const r = rng(21);
    const hum = (i: number) => {
      const f = new Float32Array(FRAME_SAMPLES);
      const a = amp(-38) * Math.SQRT2;
      for (let n = 0; n < FRAME_SAMPLES; n++)
        f[n] = a * (0.75 * Math.sin((2 * Math.PI * 120 * (i * FRAME_SAMPLES + n)) / IN_RATE) + 0.25 * (r() * 2 - 1) * Math.sqrt(3));
      return floatTo16(f).buffer as ArrayBuffer;
    };
    const sounds: ((i: number) => ArrayBuffer)[] = [
      () => noise(-38, r), // a fan switched on
      hum, // a motor
      (i) => voice(-48, i * FRAME_SAMPLES), // a monotone voice
    ];
    for (const make of sounds) {
      const g = new NoiseGate();
      run(g, Array.from({ length: 100 }, () => noise(ROOM, r)));
      const steady = Array.from({ length: 1500 }, (_, i) => make(i)); // 30 s
      expect(heard(run(g, steady), steady)).toBe(1500);
      expect(g.open).toBe(true);
      const after = run(g, Array.from({ length: 150 }, () => noise(ROOM, r)));
      expect(g.open).toBe(false);
      expect(after.slice(-50).every(isSilence)).toBe(true);
    }
  });

  test("a whisper under -55 dBFS does not open it", () => {
    const r = rng(9);
    const g = new NoiseGate();
    run(g, Array.from({ length: 100 }, () => noise(-75, r)));
    const out = run(g, Array.from({ length: 50 }, (_, i) => voice(-58, i * FRAME_SAMPLES)));
    expect(out.every(isSilence)).toBe(true);
    expect(g.open).toBe(false);
  });

  test("reset forgets the queue and the hold", () => {
    const r = rng(10);
    const g = new NoiseGate();
    run(g, Array.from({ length: 50 }, () => noise(ROOM, r)));
    run(g, [0, 1, 2].map((k) => voice(-20, k * FRAME_SAMPLES)));
    expect(g.open).toBe(true);
    g.reset();
    expect(g.open).toBe(false);
    // A fresh start holds the first frames as pre-roll again, as a new call does.
    expect(g.push(noise(ROOM, r))).toEqual([]);
  });

  for (const rate of [44_100, 48_000]) {
    test(`a ${rate} Hz device behaves the same through Framer`, () => {
      const r = rng(11);
      const framer = new Framer(rate);
      const g = new NoiseGate();
      const block = (db: number, tone: boolean, n: number) => {
        const f = new Float32Array(n);
        const a = amp(db);
        for (let i = 0; i < n; i++)
          f[i] = tone ? a * Math.SQRT2 * Math.sin((2 * Math.PI * 220 * i) / rate) : (r() * 2 - 1) * a * Math.sqrt(3);
        return f;
      };
      const out: ArrayBuffer[] = [];
      // Variable worklet batches, as the browser delivers them: 2 s of room, then a word.
      for (let t = 0; t < 2 * rate; ) {
        const n = 1024 + Math.floor(r() * 1024);
        for (const fr of framer.push(block(ROOM, false, n))) out.push(...g.push(fr));
        t += n;
      }
      expect(g.open).toBe(false);
      expect(out.every(isSilence)).toBe(true);
      for (const fr of framer.push(block(-22, true, Math.round(rate * 0.1)))) out.push(...g.push(fr));
      expect(g.open).toBe(true);
    });
  }
});
