/** A call's audio (#1397): the microphone in, HQ's speech out.
 *
 *  - The microphone is acquired by `prepare()`, which only the Call tap calls (a user gesture, so
 *    the permission prompt appears then). Its track stays DISABLED, and nothing is sent, until
 *    `start()`: the UI says "Mic · not sending" in between, never "Off".
 *  - Echo cancellation, noise suppression and gain control are the browser's: HQ's voice through
 *    the speakers must not read as the operator talking over it.
 *  - Speech plays through scheduled buffers in its OWN context at the speech's rate (24 kHz): the
 *    browser converts that one continuous stream to the device's rate, instead of resampling each
 *    40 ms chunk on its own — at a 44.1 kHz device that clicked at every chunk boundary, 25 times
 *    a second (heard as stutter). A start-up cushion that grows after an underrun absorbs network
 *    jitter (`PlaybackClock`). `flush()` (on the server's `interrupt`) stops every queued and
 *    playing buffer at once.
 *  - `mark(id, onPlayed)` (#1397 P2) puts a marker at the end of the speech queued so far;
 *    `onPlayed` runs once the audio clock has passed it. `flush()` drops pending markers, so an
 *    interrupted say never reports itself played.
 *  - `stop()` ends every track, every buffer and the context. Nothing here ever acquires the
 *    microphone again: a new call is a new tap and a new `CallAudio`. */
// A real file, never inlined: the CSP's `script-src 'self'` refuses a `data:` worklet module.
import captureWorkletUrl from "./captureWorklet.js?url&no-inline";
import {
  choose,
  goneNotice,
  listDevices,
  remembered,
  type DeviceLists,
  type DeviceOption,
} from "./audioDevices";
import { NoiseGate } from "./noiseGate";
import { Framer, OUT_RATE, pcm16ToFloat, rms } from "./pcm";
import { PlaybackClock } from "./playbackClock";
import { PlaybackMarkers } from "./playbackMarkers";

/** How often pending markers are checked against the audio clock. */
const MARK_POLL_MS = 40;

export interface CallAudio {
  prepare(): Promise<void>;
  start(send: (frame: ArrayBuffer) => void): void;
  setMuted(on: boolean): void;
  play(pcm: ArrayBuffer): void;
  /** Call `onPlayed(id)` once everything queued before this marker has played. */
  mark(id: string, onPlayed: (id: string) => void): void;
  flush(): void;
  stop(): void;
  /** 0–1, the latest block's level. */
  inputLevel(): number;
  outputLevel(): number;
}

/** Choosing the call's microphone and speaker (#1670). Errors come back as the sentence to show;
 *  `null` is success. A failed or superseded switch changes nothing. */
/** A device switch that a newer choice (or the call ending) overtook: it changed nothing and is
 *  neither a success to remember nor an error to show (#1670 review). */
export const SUPERSEDED: unique symbol = Symbol("superseded");
export type SwitchResult = string | null | typeof SUPERSEDED;

export interface DeviceAudio {
  devices(): Promise<DeviceLists>;
  /** The device ids in use; `""` is the browser's default. */
  current(): { input: string; output: string };
  /** `null`: switched; a string: why it couldn't (nothing changed); `SUPERSEDED`: overtaken. */
  setInput(id: string): Promise<SwitchResult>;
  setOutput(id: string): Promise<SwitchResult>;
  canSelectOutput(): boolean;
  /** Whether the browser offers its own speaker picker (`selectAudioOutput`), for a speaker it has
   *  not permitted yet (often not listed at all, or refused by a plain switch). */
  canPickOutput(): boolean;
  /** Open that picker and switch to the speaker chosen there. Call it straight from the tap: the
   *  picker needs the gesture, so nothing is awaited before it. A cancel changes nothing. */
  pickOutput(): Promise<{ result: SwitchResult; device: DeviceOption | null }>;
  /** Fallback notices: a remembered device gone, the device in use unplugged mid-call. */
  onNotice(fn: ((message: string) => void) | null): void;
}

export type CallAudioFactory = () => CallAudio;

const MIC: MediaTrackConstraints = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
  channelCount: 1,
};

function why(e: unknown): string {
  const name = e instanceof DOMException ? e.name : "";
  if (name === "NotAllowedError") return "the browser refused";
  if (name === "NotFoundError" || name === "OverconstrainedError") return "it isn’t connected";
  if (name === "NotReadableError") return "another app is using it";
  return "it didn’t respond";
}

export class BrowserCallAudio implements CallAudio, DeviceAudio {
  private stream: MediaStream | null = null;
  private ctx: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private framer: Framer | null = null;
  /** Room noise never reaches the worker's voice detector (#1667): silence goes in its place. */
  private readonly gate = new NoiseGate();
  private send: ((frame: ArrayBuffer) => void) | null = null;
  private muted = false;
  private stopped = false;
  /** HQ's speech plays here, at OUT_RATE (see the module note). */
  private out: AudioContext | null = null;
  private playing: AudioBufferSourceNode[] = [];
  private clock = new PlaybackClock();
  private inLevel = 0;
  private outLevels: { at: number; level: number }[] = [];
  private markers = new PlaybackMarkers();
  private onPlayed = new Map<string, (id: string) => void>();
  private markTimer: number | null = null;
  /** Bumped by every microphone switch (and by stop): a slower, older switch never lands. */
  private inputGen = 0;
  /** Microphone switches still acquiring: an unplug check stands down for them (Hermes 7013). */
  private inputPending = 0;
  /** Speaker switches run in the order they were asked; a newer one overtakes those still queued. */
  private outputGen = 0;
  private outputChain: Promise<unknown> = Promise.resolve();
  /** The last names seen per device id: what an unplug notice calls the device that is gone. */
  private labels = new Map<string, string>();
  private notify: ((message: string) => void) | null = null;
  private pendingNotices: string[] = [];
  /** Device checks run one after another: an unplug fires both the track's `ended` and
   *  `devicechange`, and the second check finds the fallback already done. */
  private deviceCheck: Promise<void> = Promise.resolve();
  private readonly onDeviceChange = () => {
    this.deviceCheck = this.deviceCheck.then(() => this.handleDeviceChange()).catch(() => undefined);
  };

  async prepare(): Promise<void> {
    if (this.stopped) throw new Error("this call's audio has ended");
    // The remembered microphone (#1670), asked for as `ideal`: if it is gone, the browser's
    // default answers instead of the call failing.
    const want = remembered("input");
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: want ? { ...MIC, deviceId: { ideal: want.id } } : MIC,
    });
    for (const t of this.stream.getAudioTracks()) t.enabled = false;
    if (this.stopped) {
      this.releaseTracks();
      throw new Error("this call's audio has ended");
    }
    const ctx = new AudioContext();
    this.ctx = ctx;
    await ctx.audioWorklet.addModule(captureWorkletUrl);
    this.endedWhileAwaiting(ctx); // `stop()` may have run while the module loaded
    this.source = ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(ctx, "battlelab-capture");
    this.framer = new Framer(ctx.sampleRate);
    this.node.port.onmessage = (e: MessageEvent<Float32Array>) => this.onBlock(e.data);
    this.source.connect(this.node);
    // The worklet must be pulled to run; a zero-gain sink keeps it from reaching the speakers.
    const sink = ctx.createGain();
    sink.gain.value = 0;
    this.node.connect(sink).connect(ctx.destination);
    if (ctx.state === "suspended") await ctx.resume();
    this.endedWhileAwaiting(ctx);
    // Playback in its own context at the speech's rate (#1397): no per-chunk resampling. A
    // browser that can't open one at that rate gets its default (and plays as before).
    const out = playbackContext();
    this.out = out;
    if (out.state === "suspended") await out.resume();
    this.endedWhileAwaiting(ctx);
    navigator.mediaDevices.addEventListener?.("devicechange", this.onDeviceChange);
    this.watchTrack();
    await this.applyRemembered();
  }

  /** The remembered choices (#1670): the speaker, and a microphone found again by its UNIQUE
   *  label after its id changed. A device that is gone: the default, and the notice says why. */
  private async applyRemembered(): Promise<void> {
    let lists: DeviceLists;
    try {
      lists = await this.devices();
    } catch {
      return;
    }
    const mic = choose("input", lists.inputs, remembered("input"));
    if (mic.id && mic.id !== this.current().input) {
      const err = await this.setInput(mic.id);
      if (typeof err === "string") this.say(err); // it would not open: the default stays, and why
    }
    if (mic.reason) this.say(mic.reason);
    const spk = choose("output", lists.outputs, remembered("output"));
    if (spk.id && this.canSelectOutput()) {
      const err = await this.setOutput(spk.id);
      if (typeof err === "string") this.say(err);
    }
    if (spk.reason) this.say(spk.reason);
  }

  private say(message: string): void {
    if (this.notify) this.notify(message);
    else this.pendingNotices.push(message);
  }

  onNotice(fn: ((message: string) => void) | null): void {
    this.notify = fn;
    if (fn) for (const m of this.pendingNotices.splice(0)) fn(m);
  }

  async devices(): Promise<DeviceLists> {
    const lists = await listDevices(navigator.mediaDevices);
    for (const d of [...lists.inputs, ...lists.outputs]) this.labels.set(d.id, d.label);
    return lists;
  }

  current(): { input: string; output: string } {
    const track = this.stream?.getAudioTracks()[0];
    const input = (track?.getSettings?.().deviceId as string | undefined) ?? "";
    // HQ's speech plays in its own context (#1397): the speaker is THAT context's sink.
    const sink = (this.out as unknown as { sinkId?: unknown } | null)?.sinkId;
    return { input, output: typeof sink === "string" ? sink : "" };
  }

  canPickOutput(): boolean {
    const md = navigator.mediaDevices as unknown as { selectAudioOutput?: unknown } | undefined;
    return this.canSelectOutput() && typeof md?.selectAudioOutput === "function";
  }

  async pickOutput(): Promise<{ result: SwitchResult; device: DeviceOption | null }> {
    const md = navigator.mediaDevices as unknown as {
      selectAudioOutput?: () => Promise<{ deviceId: string; label: string }>;
    };
    if (!this.canPickOutput() || !md.selectAudioOutput) {
      return { result: "This browser can’t open a speaker picker.", device: null };
    }
    // Its place among the speaker choices is taken NOW, at the tap: a listed speaker (or another
    // picker) chosen while this one is still open is newer, and wins (Hermes 7016).
    const gen = ++this.outputGen;
    let picked: { deviceId: string; label: string };
    try {
      picked = await md.selectAudioOutput(); // first thing: the tap's gesture is still live
    } catch (e) {
      // Cancelled (or not allowed): nothing changed, nothing to show.
      if (e instanceof DOMException && (e.name === "NotAllowedError" || e.name === "AbortError")) {
        return { result: SUPERSEDED, device: null };
      }
      return { result: `Couldn’t open the speaker picker — ${why(e)}.`, device: null };
    }
    const device = { id: picked.deviceId, label: picked.label || "Speaker" };
    this.labels.set(device.id, device.label);
    if (gen !== this.outputGen) return { result: SUPERSEDED, device: null }; // overtaken meanwhile
    const result = await this.queueSink(device.id, gen);
    return { result, device: result === SUPERSEDED ? null : device };
  }

  canSelectOutput(): boolean {
    return !!this.out && typeof (this.out as unknown as { setSinkId?: unknown }).setSinkId === "function";
  }

  /** Swap the microphone mid-call: the new one is acquired and CONNECTED before the old one is
   *  disconnected and stopped; mute and not-yet-sending carry over; the noise gate starts afresh.
   *  A newer choice, or the call ending, while this one waits: its track is stopped, never used. */
  async setInput(id: string): Promise<SwitchResult> {
    let result: SwitchResult;
    this.inputPending += 1;
    try {
      result = await this.switchInput(id);
    } finally {
      this.inputPending -= 1;
    }
    // A pick that failed while an unplug check stood down for it: check again, so a mic that is
    // gone is still replaced. Never for the fallback itself (`""`), which would loop.
    if (typeof result === "string" && id !== "") this.onDeviceChange();
    return result;
  }

  private async switchInput(id: string): Promise<SwitchResult> {
    if (this.stopped || !this.ctx || !this.node) return "The call has ended.";
    const gen = ++this.inputGen;
    let next: MediaStream;
    try {
      next = await navigator.mediaDevices.getUserMedia({
        audio: id ? { ...MIC, deviceId: { exact: id } } : MIC,
      });
    } catch (e) {
      return gen === this.inputGen ? `Couldn’t switch to that microphone — ${why(e)}.` : SUPERSEDED;
    }
    if (this.stopped || gen !== this.inputGen || !this.ctx || !this.node) {
      for (const t of next.getTracks()) t.stop();
      return SUPERSEDED;
    }
    let src: MediaStreamAudioSourceNode;
    try {
      src = this.ctx.createMediaStreamSource(next);
      src.connect(this.node);
    } catch (e) {
      // It opened but can't be wired in (a rate the browser refuses): never left running.
      for (const t of next.getTracks()) t.stop();
      return `Couldn’t switch to that microphone — ${why(e)}.`;
    }
    const oldSource = this.source;
    const oldStream = this.stream;
    this.source = src;
    this.stream = next;
    for (const t of next.getAudioTracks()) t.enabled = !!this.send && !this.muted;
    try {
      oldSource?.disconnect();
    } catch {
      /* already disconnected */
    }
    for (const t of oldStream?.getTracks() ?? []) t.stop();
    this.gate.reset();
    this.watchTrack();
    return null;
  }

  setOutput(id: string): Promise<SwitchResult> {
    return this.queueSink(id, ++this.outputGen);
  }

  /** Run a speaker switch in its place in line: skipped if a newer choice took a later `gen`. */
  private queueSink(id: string, gen: number): Promise<SwitchResult> {
    const run = this.outputChain.then(
      (): SwitchResult | Promise<SwitchResult> =>
        gen === this.outputGen ? this.applySink(id) : SUPERSEDED,
    );
    this.outputChain = run.catch(() => undefined);
    return run;
  }

  private async applySink(id: string): Promise<string | null> {
    const ctx = this.out as unknown as { setSinkId?: (id: string) => Promise<void> } | null;
    if (!ctx || this.stopped) return "The call has ended.";
    if (typeof ctx.setSinkId !== "function") {
      return "This browser can’t choose the speaker: switch it in your device’s sound settings.";
    }
    try {
      await ctx.setSinkId(id);
    } catch (e) {
      return `Couldn’t switch to that speaker — ${why(e)}. Still playing on the current one.`;
    }
    return null;
  }

  /** The device in use ended (unplugged, Bluetooth gone): the same check as a device change. */
  private watchTrack(): void {
    for (const t of this.stream?.getAudioTracks() ?? []) t.onended = this.onDeviceChange;
  }

  /** A device in use disappeared mid-call: back to the default, and the sheet says so. The call
   *  never ends for a device change; if even the default fails, what still works stays. */
  private async handleDeviceChange(): Promise<void> {
    if (this.stopped) return;
    const inputGen = this.inputGen;
    const outputGen = this.outputGen;
    let lists: DeviceLists;
    try {
      lists = await this.devices();
    } catch {
      return;
    }
    if (this.stopped) return;
    // What is in use NOW, after the wait — and only if the Commander made no choice meanwhile: a
    // pick made, or still opening, decides for itself (Hermes 7013).
    const now = this.current();
    const listed = (ids: { id: string }[], id: string) => !id || ids.some((d) => d.id === id);
    // Gone from the list, or its track ended while still listed (the browser's "default" mic
    // whose device was unplugged, a Bluetooth profile switch): back to the default either way.
    const dead = this.stream?.getAudioTracks().some((t) => t.readyState === "ended") ?? false;
    const inputUntouched = this.inputGen === inputGen && this.inputPending === 0;
    if (inputUntouched && (dead || !listed(lists.inputs, now.input))) {
      const label = this.labels.get(now.input) ?? "";
      const err = await this.setInput("");
      if (err !== SUPERSEDED) this.say(err ?? goneNotice("input", label));
    }
    if (this.outputGen === outputGen && !listed(lists.outputs, now.output)) {
      const label = this.labels.get(now.output) ?? "";
      const err = await this.setOutput("");
      if (err !== SUPERSEDED) this.say(err ?? goneNotice("output", label));
    }
  }

  /** After an await in `prepare()`: if the call ended meanwhile, build nothing more — tear down
   *  what exists (stop() already ran) and fail, so no orphaned graph outlives the call. */
  private endedWhileAwaiting(ctx: AudioContext): void {
    if (!this.stopped) return;
    try {
      this.source?.disconnect();
      this.node?.disconnect();
    } catch {
      /* already disconnected */
    }
    if (this.node) this.node.port.onmessage = null;
    this.source = null;
    this.node = null;
    void this.out?.close().catch(() => undefined);
    this.out = null;
    this.releaseTracks();
    void ctx.close().catch(() => undefined);
    throw new Error("this call's audio has ended");
  }

  private onBlock(block: Float32Array): void {
    if (!this.send || this.muted || this.stopped || !this.framer) return;
    this.inLevel = rms(block);
    for (const frame of this.framer.push(block)) for (const out of this.gate.push(frame)) this.send(out);
  }

  start(send: (frame: ArrayBuffer) => void): void {
    if (this.stopped) return;
    this.send = send;
    this.gate.reset();
    for (const t of this.stream?.getAudioTracks() ?? []) t.enabled = !this.muted;
  }

  setMuted(on: boolean): void {
    this.muted = on;
    this.gate.reset(); // a fresh start after a mute: queue and hold
    if (on) this.inLevel = 0;
    if (this.send) for (const t of this.stream?.getAudioTracks() ?? []) t.enabled = !on;
  }

  play(pcm: ArrayBuffer): void {
    const ctx = this.out;
    if (!ctx || this.stopped) return;
    const samples = pcm16ToFloat(pcm);
    if (!samples.length) return;
    const buffer = ctx.createBuffer(1, samples.length, OUT_RATE);
    buffer.copyToChannel(samples, 0);
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(ctx.destination);
    const at = this.clock.schedule(ctx.currentTime, buffer.duration);
    src.start(at);
    this.playing.push(src);
    this.outLevels.push({ at, level: rms(samples) });
    src.onended = () => {
      this.playing = this.playing.filter((p) => p !== src);
    };
  }

  mark(id: string, onPlayed: (id: string) => void): void {
    const ctx = this.out;
    if (!ctx || this.stopped) return;
    this.markers.add(id, Math.max(this.clock.end, ctx.currentTime));
    this.onPlayed.set(id, onPlayed);
    if (this.markTimer === null) {
      this.markTimer = window.setInterval(() => this.checkMarkers(), MARK_POLL_MS);
    }
  }

  private checkMarkers(): void {
    const ctx = this.out;
    // A suspended or closed context is not playing: its clock is not proof of anything.
    if (ctx && !this.stopped && ctx.state === "running") {
      for (const id of this.markers.due(ctx.currentTime)) {
        const fn = this.onPlayed.get(id);
        this.onPlayed.delete(id);
        fn?.(id);
      }
    }
    if (!this.markers.size) this.clearMarkTimer();
  }

  private clearMarkTimer(): void {
    if (this.markTimer !== null) window.clearInterval(this.markTimer);
    this.markTimer = null;
  }

  flush(): void {
    this.markers.flush();
    this.onPlayed.clear();
    this.clearMarkTimer();
    for (const src of this.playing) {
      try {
        src.stop();
      } catch {
        /* already stopped */
      }
    }
    this.playing = [];
    this.outLevels = [];
    this.clock.flush();
  }

  private releaseTracks(): void {
    for (const t of this.stream?.getTracks() ?? []) t.stop();
    this.stream = null;
  }

  stop(): void {
    this.stopped = true;
    this.inputGen++; // a switch still waiting for its track lands nowhere
    navigator.mediaDevices?.removeEventListener?.("devicechange", this.onDeviceChange);
    this.notify = null;
    this.send = null;
    this.flush();
    try {
      this.source?.disconnect();
      this.node?.disconnect();
    } catch {
      /* already disconnected */
    }
    if (this.node) this.node.port.onmessage = null;
    this.releaseTracks();
    void this.ctx?.close().catch(() => undefined);
    this.ctx = null;
    void this.out?.close().catch(() => undefined);
    this.out = null;
    this.inLevel = 0;
  }

  inputLevel(): number {
    return this.muted ? 0 : this.inLevel;
  }

  outputLevel(): number {
    const now = this.out?.currentTime ?? 0;
    this.outLevels = this.outLevels.filter((l) => l.at + 0.25 > now);
    const current = this.outLevels.filter((l) => l.at <= now).pop();
    return this.playing.length ? (current?.level ?? 0) : 0;
  }
}

/** A context at the speech's own rate, so its chunks are never resampled one by one; the
 *  browser's default where a rate can't be chosen. */
function playbackContext(): AudioContext {
  try {
    return new AudioContext({ sampleRate: OUT_RATE, latencyHint: "interactive" });
  } catch {
    return new AudioContext();
  }
}

export const browserCallAudio: CallAudioFactory = () => new BrowserCallAudio();
