/** A call's audio (#1397): the microphone in, HQ's speech out.
 *
 *  - The microphone is acquired by `prepare()`, which only the Call tap calls (a user gesture, so
 *    the permission prompt appears then). Its track stays DISABLED, and nothing is sent, until
 *    `start()`: the UI says "Mic · not sending" in between, never "Off".
 *  - Echo cancellation, noise suppression and gain control are the browser's: HQ's voice through
 *    the speakers must not read as the operator talking over it.
 *  - Speech plays through scheduled buffers; `flush()` (on the server's `interrupt`) stops every
 *    queued and playing buffer at once.
 *  - `mark(id, onPlayed)` (#1397 P2) puts a marker at the end of the speech queued so far;
 *    `onPlayed` runs once the audio clock has passed it. `flush()` drops pending markers, so an
 *    interrupted say never reports itself played.
 *  - `stop()` ends every track, every buffer and the context. Nothing here ever acquires the
 *    microphone again: a new call is a new tap and a new `CallAudio`. */
// A real file, never inlined: the CSP's `script-src 'self'` refuses a `data:` worklet module.
import captureWorkletUrl from "./captureWorklet.js?url&no-inline";
import { Framer, OUT_RATE, pcm16ToFloat, rms } from "./pcm";
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

export type CallAudioFactory = () => CallAudio;

export class BrowserCallAudio implements CallAudio {
  private stream: MediaStream | null = null;
  private ctx: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private framer: Framer | null = null;
  private send: ((frame: ArrayBuffer) => void) | null = null;
  private muted = false;
  private stopped = false;
  private playing: AudioBufferSourceNode[] = [];
  private nextAt = 0;
  private inLevel = 0;
  private outLevels: { at: number; level: number }[] = [];
  private markers = new PlaybackMarkers();
  private onPlayed = new Map<string, (id: string) => void>();
  private markTimer: number | null = null;

  async prepare(): Promise<void> {
    if (this.stopped) throw new Error("this call's audio has ended");
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
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
    this.releaseTracks();
    void ctx.close().catch(() => undefined);
    throw new Error("this call's audio has ended");
  }

  private onBlock(block: Float32Array): void {
    if (!this.send || this.muted || this.stopped || !this.framer) return;
    this.inLevel = rms(block);
    for (const frame of this.framer.push(block)) this.send(frame);
  }

  start(send: (frame: ArrayBuffer) => void): void {
    if (this.stopped) return;
    this.send = send;
    for (const t of this.stream?.getAudioTracks() ?? []) t.enabled = !this.muted;
  }

  setMuted(on: boolean): void {
    this.muted = on;
    if (on) this.inLevel = 0;
    if (this.send) for (const t of this.stream?.getAudioTracks() ?? []) t.enabled = !on;
  }

  play(pcm: ArrayBuffer): void {
    const ctx = this.ctx;
    if (!ctx || this.stopped) return;
    const samples = pcm16ToFloat(pcm);
    if (!samples.length) return;
    const buffer = ctx.createBuffer(1, samples.length, OUT_RATE);
    buffer.copyToChannel(samples, 0);
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(ctx.destination);
    const at = Math.max(ctx.currentTime + 0.03, this.nextAt);
    src.start(at);
    this.nextAt = at + buffer.duration;
    this.playing.push(src);
    this.outLevels.push({ at, level: rms(samples) });
    src.onended = () => {
      this.playing = this.playing.filter((p) => p !== src);
    };
  }

  mark(id: string, onPlayed: (id: string) => void): void {
    const ctx = this.ctx;
    if (!ctx || this.stopped) return;
    this.markers.add(id, Math.max(this.nextAt, ctx.currentTime));
    this.onPlayed.set(id, onPlayed);
    if (this.markTimer === null) {
      this.markTimer = window.setInterval(() => this.checkMarkers(), MARK_POLL_MS);
    }
  }

  private checkMarkers(): void {
    const ctx = this.ctx;
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
    this.nextAt = 0;
  }

  private releaseTracks(): void {
    for (const t of this.stream?.getTracks() ?? []) t.stop();
    this.stream = null;
  }

  stop(): void {
    this.stopped = true;
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
    this.inLevel = 0;
  }

  inputLevel(): number {
    return this.muted ? 0 : this.inLevel;
  }

  outputLevel(): number {
    const now = this.ctx?.currentTime ?? 0;
    this.outLevels = this.outLevels.filter((l) => l.at + 0.25 > now);
    const current = this.outLevels.filter((l) => l.at <= now).pop();
    return this.playing.length ? (current?.level ?? 0) : 0;
  }
}

export const browserCallAudio: CallAudioFactory = () => new BrowserCallAudio();
