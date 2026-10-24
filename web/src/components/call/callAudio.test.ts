/** The browser audio engine's playback markers (#1397 P2), against a fake Web Audio clock: `played`
 *  is reported only once the speech queued before the marker has played out — never for a flushed
 *  say, and never while the context is not running. */
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { BrowserCallAudio } from "./callAudio";

class FakeContext {
  static last: FakeContext;
  currentTime = 0;
  state: "running" | "suspended" | "closed" = "running";
  sampleRate = 48_000;
  destination = {};
  started: { at: number; stopped: boolean }[] = [];
  audioWorklet = { addModule: async () => undefined };
  constructor() {
    FakeContext.last = this;
  }
  createMediaStreamSource() {
    return { connect: () => undefined, disconnect: () => undefined };
  }
  createGain() {
    return { gain: { value: 1 }, connect: (x: unknown) => x };
  }
  createBuffer(_ch: number, length: number, rate: number) {
    return { duration: length / rate, copyToChannel: () => undefined };
  }
  createBufferSource() {
    const rec = { at: -1, stopped: false };
    this.started.push(rec);
    return {
      buffer: null,
      onended: null,
      connect: () => undefined,
      start: (at: number) => {
        rec.at = at;
      },
      stop: () => {
        rec.stopped = true;
      },
    };
  }
  async resume() {
    this.state = "running";
  }
  async close() {
    this.state = "closed";
  }
}

class FakeWorkletNode {
  port = { onmessage: null };
  connect(x: unknown) {
    return x;
  }
  disconnect() {}
}

/** One second of speech at 24 kHz, PCM16. */
const SECOND = new ArrayBuffer(24_000 * 2);

let audio: BrowserCallAudio;

beforeEach(async () => {
  vi.useFakeTimers();
  vi.stubGlobal("AudioContext", FakeContext);
  vi.stubGlobal("AudioWorkletNode", FakeWorkletNode);
  const track = { enabled: true, stop: () => undefined };
  vi.stubGlobal("navigator", {
    mediaDevices: {
      getUserMedia: async () => ({ getAudioTracks: () => [track], getTracks: () => [track] }),
    },
  });
  audio = new BrowserCallAudio();
  await audio.prepare();
});

afterEach(() => {
  audio.stop();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("the marker acks only after the audio queued before it has played", () => {
  const ctx = FakeContext.last;
  audio.play(SECOND);
  audio.play(SECOND);
  const played = vi.fn();
  audio.mark("s1", played);
  ctx.currentTime = 1.5; // half-way through the second buffer
  vi.advanceTimersByTime(200);
  expect(played).not.toHaveBeenCalled();
  ctx.currentTime = 2.04; // both buffers (scheduled 30 ms ahead, ending at 2.03 s) have played
  vi.advanceTimersByTime(200);
  expect(played).toHaveBeenCalledExactlyOnceWith("s1");
  vi.advanceTimersByTime(1000);
  expect(played).toHaveBeenCalledTimes(1);
});

test("an interrupt's flush drops the marker: a flushed say never acks, even when later audio plays", () => {
  const ctx = FakeContext.last;
  audio.play(SECOND);
  const played = vi.fn();
  audio.mark("s1", played);
  ctx.currentTime = 0.4;
  audio.flush();
  expect(ctx.started.every((b) => b.stopped)).toBe(true);
  audio.play(SECOND); // the next answer's audio, and its marker (which restarts the polling)
  const next = vi.fn();
  audio.mark("s2", next);
  ctx.currentTime = 10;
  vi.advanceTimersByTime(500);
  expect(next).toHaveBeenCalledExactlyOnceWith("s2");
  expect(played).not.toHaveBeenCalled();
});

test("a marker behind a later say still waits for that say's audio", () => {
  const ctx = FakeContext.last;
  audio.play(SECOND);
  const first = vi.fn();
  audio.mark("a", first);
  audio.play(SECOND);
  const second = vi.fn();
  audio.mark("b", second);
  ctx.currentTime = 1.1;
  vi.advanceTimersByTime(100);
  expect(first).toHaveBeenCalledOnce();
  expect(second).not.toHaveBeenCalled();
  ctx.currentTime = 2.1;
  vi.advanceTimersByTime(100);
  expect(second).toHaveBeenCalledOnce();
});

test("a suspended context is not playing: no ack until it runs again", () => {
  const ctx = FakeContext.last;
  audio.play(SECOND);
  const played = vi.fn();
  audio.mark("s1", played);
  ctx.state = "suspended";
  ctx.currentTime = 5;
  vi.advanceTimersByTime(500);
  expect(played).not.toHaveBeenCalled();
  ctx.state = "running";
  vi.advanceTimersByTime(100);
  expect(played).toHaveBeenCalledOnce();
});

test("stopping the call drops every marker", () => {
  const ctx = FakeContext.last;
  audio.play(SECOND);
  const played = vi.fn();
  audio.mark("s1", played);
  audio.stop();
  ctx.currentTime = 5;
  vi.advanceTimersByTime(500);
  expect(played).not.toHaveBeenCalled();
  audio.mark("s2", played); // after stop: ignored
  vi.advanceTimersByTime(500);
  expect(played).not.toHaveBeenCalled();
});

test("a hang-up while the worklet module loads builds no audio graph after it (#1640 review)", async () => {
  let loaded!: () => void;
  const sources: unknown[] = [];
  class SlowContext extends FakeContext {
    audioWorklet = { addModule: () => new Promise<undefined>((r) => (loaded = () => r(undefined))) };
    createMediaStreamSource() {
      const s = { connect: () => undefined, disconnect: () => undefined };
      sources.push(s);
      return s;
    }
  }
  vi.stubGlobal("AudioContext", SlowContext);
  const late = new BrowserCallAudio();
  const preparing = late.prepare();
  await vi.waitFor(() => expect(loaded).toBeTypeOf("function"));
  late.stop(); // the call ended while the module was still loading
  loaded();
  await expect(preparing).rejects.toThrow("ended");
  expect(sources).toEqual([]); // nothing was built (or connected) after the stop
});
