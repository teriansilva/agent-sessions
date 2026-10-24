import { describe, expect, test } from "vitest";

import { FRAME_SAMPLES, Framer, floatTo16, pcm16ToFloat, rms } from "./pcm";

describe("PCM conversion (#1397)", () => {
  test("floats become clamped 16-bit samples", () => {
    expect(Array.from(floatTo16(Float32Array.from([0, 1, -1, 2, -2, 0.5])))).toEqual([
      0, 32767, -32768, 32767, -32768, 16384,
    ]);
  });

  test("16-bit bytes become floats; an odd trailing byte is ignored", () => {
    const pcm = new Int16Array([0, 16384, -32768, 32767]);
    const bytes = new Uint8Array(pcm.length * 2 + 1);
    bytes.set(new Uint8Array(pcm.buffer));
    const out = pcm16ToFloat(bytes.buffer);
    expect(out.length).toBe(4);
    expect(out[1]).toBeCloseTo(0.5, 4);
    expect(out[2]).toBe(-1);
  });

  test("rms of silence is 0, of a full-scale square is 1", () => {
    expect(rms(new Float32Array(10))).toBe(0);
    expect(rms(Float32Array.from([1, -1, 1, -1]))).toBe(1);
  });
});

describe("Framer: device rate → 16 kHz, 640-byte frames", () => {
  const sine = (rate: number, seconds: number) => {
    const n = Math.round(rate * seconds);
    return Float32Array.from({ length: n }, (_, i) => Math.sin((2 * Math.PI * 440 * i) / rate) * 0.5);
  };

  test.each([48_000, 44_100, 16_000])("one second at %i Hz is ~50 frames of 20 ms", (rate) => {
    const f = new Framer(rate);
    const frames = f.push(sine(rate, 1));
    expect(frames.length).toBeGreaterThanOrEqual(49);
    expect(frames.length).toBeLessThanOrEqual(50);
    for (const fr of frames) expect(fr.byteLength).toBe(FRAME_SAMPLES * 2);
  });

  test("odd block sizes give the same frames as one block (the read position carries over)", () => {
    const signal = sine(48_000, 0.5);
    const whole = new Framer(48_000).push(signal);
    const parts = new Framer(48_000);
    const got: ArrayBuffer[] = [];
    for (let i = 0; i < signal.length; i += 127) got.push(...parts.push(signal.subarray(i, i + 127)));
    expect(got.length).toBe(whole.length);
    const a = new Int16Array(whole[10]);
    const b = new Int16Array(got[10]);
    for (let i = 0; i < a.length; i++) expect(Math.abs(a[i] - b[i])).toBeLessThanOrEqual(1);
  });

  test("a bad rate is refused", () => {
    expect(() => new Framer(0)).toThrow();
  });
});
