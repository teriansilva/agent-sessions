/** The browser audio engine's playback markers (#1397 P2), against a fake Web Audio clock: `played`
 *  is reported only once the speech queued before the marker has played out — never for a flushed
 *  say, and never while the context is not running. */
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { BrowserCallAudio } from "./callAudio";
import { PLAYBACK } from "./playbackClock";

class FakeContext {
  static last: FakeContext;
  static made: FakeContext[] = [];
  options: AudioContextOptions | undefined;
  currentTime = 0;
  state: "running" | "suspended" | "closed" = "running";
  sampleRate = 48_000;
  destination = {};
  started: { at: number; stopped: boolean }[] = [];
  audioWorklet = { addModule: async () => undefined };
  constructor(options?: AudioContextOptions) {
    this.options = options;
    if (options?.sampleRate) this.sampleRate = options.sampleRate;
    FakeContext.last = this;
    FakeContext.made.push(this);
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
  // Both buffers (scheduled the start-up cushion ahead, ending at 2 s + cushion) have played.
  ctx.currentTime = 2 + PLAYBACK.startS + 0.01;
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
  ctx.currentTime = 1 + PLAYBACK.startS + 0.01; // the first say (after the cushion) has played
  vi.advanceTimersByTime(100);
  expect(first).toHaveBeenCalledOnce();
  expect(second).not.toHaveBeenCalled();
  ctx.currentTime = 2 + PLAYBACK.startS + 0.01;
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

test("HQ's speech plays in its own context at the speech's rate — never resampled chunk by chunk", async () => {
  // The capture context runs at the device's rate; playback opens at 24 kHz, so 40 ms chunks are
  // never converted one by one (that clicked at every boundary on 44.1 kHz devices).
  const [capture, playback] = FakeContext.made.slice(-2);
  expect(capture.options?.sampleRate).toBeUndefined();
  expect(playback.options?.sampleRate).toBe(24_000);
  audio.play(SECOND);
  expect(playback.started).toHaveLength(1);
  expect(capture.started).toHaveLength(0);
});

test("a browser that can't open a 24 kHz context plays in its default one", async () => {
  class PickyContext extends FakeContext {
    constructor(options?: AudioContextOptions) {
      if (options?.sampleRate) throw new DOMException("unsupported rate", "NotSupportedError");
      super(options);
    }
  }
  vi.stubGlobal("AudioContext", PickyContext);
  const a = new BrowserCallAudio();
  await a.prepare();
  a.play(SECOND);
  expect(PickyContext.last.started).toHaveLength(1);
  a.stop();
});
