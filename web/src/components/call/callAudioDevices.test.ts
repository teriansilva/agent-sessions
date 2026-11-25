/** #1670 — switching the call's microphone and speaker mid-call, against fake browser devices:
 *  the new mic is connected BEFORE the old one stops, a failed or superseded switch changes
 *  nothing, a refused speaker keeps playing where it was, and an unplugged device falls back. */
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { remember } from "./audioDevices";
import { BrowserCallAudio, SUPERSEDED } from "./callAudio";

const log: string[] = [];

class FakeTrack {
  enabled = true;
  stopped = false;
  readyState: "live" | "ended" = "live";
  onended: (() => void) | null = null;
  constructor(public deviceId: string) {}
  stop() {
    this.stopped = true;
    log.push(`stop:${this.deviceId}`);
  }
  getSettings() {
    return { deviceId: this.deviceId };
  }
}
class FakeStream {
  constructor(public track: FakeTrack) {}
  getAudioTracks() {
    return [this.track];
  }
  getTracks() {
    return [this.track];
  }
}

let devices: { kind: string; deviceId: string; label: string }[] = [];
const listeners = new Set<() => void>();
/** Pending getUserMedia calls by device id, resolved (or rejected) by the test. */
let pending: Map<string, { resolve: (s: FakeStream) => void; reject: (e: Error) => void }>;
let autoResolve = true;
/** The browser's speaker picker, per test (`selectAudioOutput`). */
let picker: () => Promise<{ deviceId: string; label: string }>;
/** While set, `enumerateDevices` waits for it (a slow device list). */
let enumGate: Promise<void> | null = null;
/** Every device id `getUserMedia` was asked for, in order. */
let opened: string[] = [];

class FakeContext {
  static last: FakeContext;
  static made: FakeContext[] = [];
  state = "running";
  sampleRate = 48_000;
  currentTime = 0;
  destination = {};
  sinkId: string = "";
  sinkFails = false;
  sourceFails = false;
  audioWorklet = { addModule: async () => undefined };
  constructor() {
    FakeContext.last = this;
    FakeContext.made.push(this);
  }
  createMediaStreamSource(stream: FakeStream) {
    if (this.sourceFails) throw new DOMException("rate", "NotSupportedError");
    const id = stream.track.deviceId;
    return {
      connect: () => log.push(`connect:${id}`),
      disconnect: () => log.push(`disconnect:${id}`),
    };
  }
  createGain() {
    return { gain: { value: 1 }, connect: (x: unknown) => x };
  }
  /** Each call waits for the next gate in this list, if any (an asynchronous sink switch). */
  sinkGates: Promise<void>[] = [];
  async setSinkId(id: string) {
    const gate = this.sinkGates.shift();
    if (gate) await gate;
    if (this.sinkFails) throw new DOMException("refused", "NotAllowedError");
    this.sinkId = id;
  }
  async resume() {}
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

function idOf(c: MediaStreamConstraints): string {
  const a = c.audio as { deviceId?: { exact?: string; ideal?: string } };
  return a.deviceId?.exact ?? a.deviceId?.ideal ?? "default";
}

beforeEach(() => {
  log.length = 0;
  localStorage.clear();
  pending = new Map();
  autoResolve = true;
  opened = [];
  enumGate = null;
  picker = async () => ({ deviceId: "bt", label: "AirPods Pro" });
  devices = [
    { kind: "audioinput", deviceId: "default", label: "Default — MacBook Pro Microphone" },
    { kind: "audioinput", deviceId: "jabra", label: "Jabra Evolve2 65" },
    { kind: "audioinput", deviceId: "shure", label: "Shure MV7" },
    { kind: "audiooutput", deviceId: "spk", label: "MacBook Pro Speakers" },
    { kind: "audiooutput", deviceId: "lg", label: "LG UltraFine Display Audio" },
  ];
  listeners.clear();
  vi.stubGlobal("AudioContext", FakeContext);
  vi.stubGlobal("AudioWorkletNode", FakeWorkletNode);
  vi.stubGlobal("navigator", {
    mediaDevices: {
      getUserMedia: (c: MediaStreamConstraints) => {
        const id = idOf(c);
        opened.push(id);
        if (autoResolve) return Promise.resolve(new FakeStream(new FakeTrack(id)));
        return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
      },
      selectAudioOutput: (() => picker()) as unknown,
      enumerateDevices: async () => {
        if (enumGate) await enumGate;
        return devices;
      },
      addEventListener: (_: string, fn: () => void) => listeners.add(fn),
      removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
    },
  });
});

afterEach(() => vi.unstubAllGlobals());

async function started(): Promise<BrowserCallAudio> {
  const a = new BrowserCallAudio();
  await a.prepare();
  a.start(() => undefined);
  log.length = 0;
  return a;
}

test("switching the mic connects the new one BEFORE the old one stops, and keeps it live", async () => {
  const a = await started();
  expect(await a.setInput("jabra")).toBeNull();
  expect(log).toEqual(["connect:jabra", "disconnect:default", "stop:default"]);
  expect(a.current().input).toBe("jabra");
  a.stop();
});

test("a muted call stays muted on the new mic", async () => {
  const a = await started();
  a.setMuted(true);
  await a.setInput("jabra");
  expect(a.current().input).toBe("jabra");
  const track = (a as unknown as { stream: FakeStream }).stream.track;
  expect(track.enabled).toBe(false);
  a.stop();
});

test("a failed switch keeps the current mic and says why", async () => {
  const a = await started();
  autoResolve = false;
  const p = a.setInput("shure");
  pending.get("shure")!.reject(new DOMException("busy", "NotReadableError"));
  expect(await p).toMatch(/Couldn’t switch to that microphone/);
  expect(a.current().input).toBe("default");
  expect(log).toEqual([]); // nothing connected, nothing stopped
  a.stop();
});

test("a newer choice wins; the slower earlier one is stopped when it lands", async () => {
  const a = await started();
  autoResolve = false;
  const first = a.setInput("jabra");
  const second = a.setInput("shure");
  pending.get("shure")!.resolve(new FakeStream(new FakeTrack("shure")));
  await second;
  pending.get("jabra")!.resolve(new FakeStream(new FakeTrack("jabra")));
  await first;
  expect(a.current().input).toBe("shure");
  expect(log).toContain("stop:jabra"); // the late track never stays open
  expect(log).not.toContain("connect:jabra");
  a.stop();
});

test("a call that ends mid-switch keeps nothing it acquired afterwards", async () => {
  const a = await started();
  autoResolve = false;
  const p = a.setInput("jabra");
  a.stop();
  log.length = 0;
  pending.get("jabra")!.resolve(new FakeStream(new FakeTrack("jabra")));
  await p;
  expect(log).toEqual(["stop:jabra"]);
});

test("the speaker chosen is the one HQ's speech plays on — its own playback context (#1397)", async () => {
  FakeContext.made = [];
  const a = await started();
  const [capture, playback] = FakeContext.made;
  expect(await a.setOutput("lg")).toBeNull();
  expect(playback.sinkId).toBe("lg");
  expect(capture.sinkId).toBe(""); // the mic's context never carries the speech
  a.stop();
});

test("a refused speaker switch keeps playing where it was, and says so", async () => {
  const a = await started();
  expect(await a.setOutput("lg")).toBeNull();
  expect(a.current().output).toBe("lg");
  FakeContext.last.sinkFails = true;
  expect(await a.setOutput("spk")).toMatch(/Couldn’t switch to that speaker.*Still playing/);
  expect(a.current().output).toBe("lg");
  a.stop();
});

test("where the browser can't pick a speaker, it says so instead of offering one", async () => {
  const a = await started();
  expect(a.canSelectOutput()).toBe(true);
  delete (FakeContext.prototype as unknown as { setSinkId?: unknown }).setSinkId;
  const b = await started();
  expect(b.canSelectOutput()).toBe(false);
  expect(await b.setOutput("lg")).toMatch(/can’t choose the speaker/);
  a.stop();
  b.stop();
  (FakeContext.prototype as unknown as { setSinkId: unknown }).setSinkId = async function (this: FakeContext, id: string) {
    if (this.sinkFails) throw new DOMException("refused", "NotAllowedError");
    this.sinkId = id;
  };
});

test("unplugging the mic in use mid-call falls back to the default and says so", async () => {
  const a = await started();
  const notices: string[] = [];
  a.onNotice((m) => notices.push(m));
  await a.setInput("jabra");
  log.length = 0;
  devices = devices.filter((d) => d.deviceId !== "jabra");
  for (const fn of listeners) fn();
  await vi.waitFor(() => expect(a.current().input).toBe("default"));
  expect(notices).toEqual(["Jabra Evolve2 65 was disconnected — now using the default microphone."]);
  expect(log).toEqual(["connect:default", "disconnect:jabra", "stop:jabra"]);
  a.stop();
});

test("unplugging the speaker in use goes back to the default output", async () => {
  const a = await started();
  const notices: string[] = [];
  a.onNotice((m) => notices.push(m));
  await a.setOutput("lg");
  devices = devices.filter((d) => d.deviceId !== "lg");
  for (const fn of listeners) fn();
  await vi.waitFor(() => expect(a.current().output).toBe(""));
  expect(notices).toEqual(["LG UltraFine Display Audio was disconnected — now using the default speaker."]);
  a.stop();
});

test("the remembered speaker is used on the next call; a gone one falls back with the reason", async () => {
  remember("output", { id: "lg", label: "LG UltraFine Display Audio" });
  const a = await started();
  expect(a.current().output).toBe("lg");
  a.stop();
  remember("output", { id: "gone", label: "AirPods Pro" });
  const b = new BrowserCallAudio();
  const notices: string[] = [];
  b.onNotice((m) => notices.push(m));
  await b.prepare();
  expect(b.current().output).toBe("");
  expect(notices).toEqual(["AirPods Pro isn't connected — using the default speaker."]);
  b.stop();
});

test("stopping the call stops listening for device changes", async () => {
  const a = await started();
  expect(listeners.size).toBe(1);
  a.stop();
  expect(listeners.size).toBe(0);
});

// --- the independent review of #1678 ------------------------------------------------------------

test("an overtaken choice says so, never success — whether it lands late or fails late", async () => {
  const a = await started();
  autoResolve = false;
  const first = a.setInput("jabra");
  const second = a.setInput("shure");
  pending.get("shure")!.resolve(new FakeStream(new FakeTrack("shure")));
  expect(await second).toBeNull();
  pending.get("jabra")!.resolve(new FakeStream(new FakeTrack("jabra")));
  expect(await first).toBe(SUPERSEDED);
  const third = a.setInput("jabra");
  const fourth = a.setInput("default");
  pending.get("default")!.resolve(new FakeStream(new FakeTrack("default")));
  await fourth;
  pending.get("jabra")!.reject(new DOMException("busy", "NotReadableError"));
  expect(await third).toBe(SUPERSEDED);
  a.stop();
});

test("the default mic whose device was unplugged (its track ended, still listed) is reopened, once", async () => {
  const a = await started();
  const notices: string[] = [];
  a.onNotice((m) => notices.push(m));
  const track = (a as unknown as { stream: FakeStream }).stream.track;
  expect(track.deviceId).toBe("default");
  track.readyState = "ended";
  log.length = 0;
  track.onended?.(); // the unplug: the track ends …
  for (const fn of listeners) fn(); // … and devicechange fires too
  const before = opened.length;
  await vi.waitFor(() => expect(log).toContain("connect:default"));
  await new Promise((r) => setTimeout(r, 0));
  expect(log.filter((l) => l === "connect:default")).toHaveLength(1);
  expect(opened.length - before).toBe(1); // one check reopens it; the second finds it done
  expect(notices).toHaveLength(1);
  expect(notices[0]).toMatch(/disconnected — now using the default microphone/);
  a.stop();
});

test("a mic that opens but can't be wired in is stopped at once, and the current one stays", async () => {
  FakeContext.made = [];
  const a = await started();
  FakeContext.made[0].sourceFails = true; // the capture context
  const err = await a.setInput("jabra");
  expect(err).toMatch(/Couldn’t switch to that microphone/);
  expect(log).toContain("stop:jabra");
  expect(a.current().input).toBe("default");
  a.stop();
});

test("a remembered mic that won't open falls back to the default, and says why", async () => {
  remember("input", { id: "jabra", label: "Jabra Evolve2 65" });
  autoResolve = false;
  const b = new BrowserCallAudio();
  const notices: string[] = [];
  b.onNotice((m) => notices.push(m));
  const ready = b.prepare();
  // Asked for as `ideal` first: the browser answers with its default, the Jabra being busy …
  await vi.waitFor(() => expect(pending.has("jabra")).toBe(true));
  pending.get("jabra")!.resolve(new FakeStream(new FakeTrack("default")));
  pending.delete("jabra");
  // … then the remembered choice is applied exactly, and that fails too.
  await vi.waitFor(() => expect(pending.has("jabra")).toBe(true));
  pending.get("jabra")!.reject(new DOMException("busy", "NotReadableError"));
  await ready;
  expect(b.current().input).toBe("default");
  expect(notices.some((m) => /Couldn’t switch to that microphone/.test(m))).toBe(true);
  b.stop();
});

test("a pick made while the unplug fallback is still opening wins, with no 'now the default' notice", async () => {
  const a = await started();
  const notices: string[] = [];
  a.onNotice((m) => notices.push(m));
  await a.setInput("jabra");
  autoResolve = false;
  devices = devices.filter((d) => d.deviceId !== "jabra"); // the Jabra is unplugged …
  for (const fn of listeners) fn();
  await vi.waitFor(() => expect(pending.has("default")).toBe(true)); // … the fallback is opening
  const pick = a.setInput("shure"); // … and the Commander picks the Shure meanwhile
  pending.get("shure")!.resolve(new FakeStream(new FakeTrack("shure")));
  expect(await pick).toBeNull();
  pending.get("default")!.resolve(new FakeStream(new FakeTrack("default")));
  await new Promise((r) => setTimeout(r, 0));
  expect(a.current().input).toBe("shure");
  expect(notices).toEqual([]);
  a.stop();
});

// --- Hermes 7013: an unplug check never overrides a newer choice; speaker picks keep their order ---

function deferred() {
  let release!: () => void;
  const done = new Promise<void>((r) => (release = r));
  return { done, release };
}

test("a pick made while an unplug check waits for the device list is kept", async () => {
  const a = await started();
  const notices: string[] = [];
  a.onNotice((m) => notices.push(m));
  await a.setInput("jabra");
  const slow = deferred();
  enumGate = slow.done;
  devices = devices.filter((d) => d.deviceId !== "jabra"); // unplugged …
  for (const fn of listeners) fn(); // … the check waits for the list …
  expect(await a.setInput("shure")).toBeNull(); // … and the Commander picks the Shure
  slow.release();
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  expect(a.current().input).toBe("shure");
  expect(notices).toEqual([]);
  a.stop();
});

test("an unplug check stands down for a pick still opening; if that pick fails, it checks again", async () => {
  const a = await started();
  const notices: string[] = [];
  a.onNotice((m) => notices.push(m));
  await a.setInput("jabra");
  autoResolve = false;
  const pick = a.setInput("shure"); // still opening …
  devices = devices.filter((d) => d.deviceId !== "jabra");
  for (const fn of listeners) fn(); // … when the Jabra is unplugged
  await new Promise((r) => setTimeout(r, 0));
  expect(pending.has("default")).toBe(false); // the check stood down for the pick
  pending.get("shure")!.reject(new DOMException("busy", "NotReadableError"));
  expect(await pick).toMatch(/Couldn’t switch/);
  await vi.waitFor(() => expect(pending.has("default")).toBe(true)); // checked again: Jabra gone
  pending.get("default")!.resolve(new FakeStream(new FakeTrack("default")));
  await vi.waitFor(() => expect(a.current().input).toBe("default"));
  expect(notices).toEqual(["Jabra Evolve2 65 was disconnected — now using the default microphone."]);
  a.stop();
});

test("speaker picks apply in the order they were made; one overtaken before its turn is skipped", async () => {
  FakeContext.made = [];
  const a = await started();
  const playback = FakeContext.made[1];
  const slow = deferred();
  playback.sinkGates = [slow.done]; // the first switch (to LG) is slow
  const toLg = a.setOutput("lg");
  await new Promise((r) => setTimeout(r, 0)); // it has started: the browser is switching
  const toMid = a.setOutput("spk"); // overtaken before its turn …
  const toLgAgain = a.setOutput("lg"); // … by this, the last pick
  slow.release();
  expect(await toLg).toBeNull();
  expect(await toMid).toBe(SUPERSEDED);
  expect(await toLgAgain).toBeNull();
  expect(playback.sinkId).toBe("lg");
  // And out of order the other way round: the last pick still wins.
  const slow2 = deferred();
  playback.sinkGates = [slow2.done];
  const first = a.setOutput("spk");
  await new Promise((r) => setTimeout(r, 0));
  const last = a.setOutput("lg");
  slow2.release();
  await Promise.all([first, last]);
  expect(playback.sinkId).toBe("lg");
  a.stop();
});

test("picks made in one go collapse into the last one: only it is applied", async () => {
  FakeContext.made = [];
  const a = await started();
  const playback = FakeContext.made[1];
  const results = await Promise.all([a.setOutput("lg"), a.setOutput("spk"), a.setOutput("lg")]);
  expect(results).toEqual([SUPERSEDED, SUPERSEDED, null]);
  expect(playback.sinkId).toBe("lg");
  a.stop();
});

// --- review 7015: the browser's own speaker picker, for a speaker it has not permitted ----------

test("the picker's speaker becomes the one HQ plays on, by the id the picker returned", async () => {
  FakeContext.made = [];
  const a = await started();
  expect(a.canPickOutput()).toBe(true);
  const { result, device } = await a.pickOutput();
  expect(result).toBeNull();
  expect(device).toEqual({ id: "bt", label: "AirPods Pro" });
  expect(FakeContext.made[1].sinkId).toBe("bt");
  a.stop();
});

test("a cancelled or refused picker changes nothing and is no error; another failure says why", async () => {
  FakeContext.made = [];
  const a = await started();
  await a.setOutput("lg");
  picker = async () => {
    throw new DOMException("dismissed", "NotAllowedError");
  };
  expect(await a.pickOutput()).toEqual({ result: SUPERSEDED, device: null });
  expect(FakeContext.made[1].sinkId).toBe("lg");
  picker = async () => {
    throw new DOMException("no devices", "NotFoundError");
  };
  const { result } = await a.pickOutput();
  expect(result).toMatch(/Couldn’t open the speaker picker/);
  expect(FakeContext.made[1].sinkId).toBe("lg");
  a.stop();
});

test("no picker where the browser can't switch the speaker at all", async () => {
  const proto = FakeContext.prototype as unknown as { setSinkId?: unknown };
  const original = proto.setSinkId;
  delete proto.setSinkId;
  try {
    const a = await started();
    expect(a.canPickOutput()).toBe(false);
    a.stop();
  } finally {
    proto.setSinkId = original;
  }
});

test("a speaker chosen while the browser's picker is still open wins over what the picker returns", async () => {
  FakeContext.made = [];
  const a = await started();
  const playback = FakeContext.made[1];
  let answer!: (d: { deviceId: string; label: string }) => void;
  picker = () => new Promise((r) => (answer = r));
  const fromPicker = a.pickOutput(); // the picker is open …
  expect(await a.setOutput("lg")).toBeNull(); // … the Commander taps the LG meanwhile
  answer({ deviceId: "bt", label: "AirPods Pro" }); // … then the picker closes on the AirPods
  expect(await fromPicker).toEqual({ result: SUPERSEDED, device: null });
  expect(playback.sinkId).toBe("lg");
  a.stop();
});
