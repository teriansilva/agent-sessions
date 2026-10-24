/** PCM helpers for a call (#1397): the mic's float samples become 16 kHz, 16-bit, 20 ms frames for
 *  the server, and the server's 24 kHz 16-bit speech becomes floats for Web Audio. Pure functions,
 *  so the conversions are unit-tested without an audio device. */

/** What the server's worker hears: 16 kHz mono PCM16. */
export const IN_RATE = 16_000;
/** What it speaks: 24 kHz mono PCM16. */
export const OUT_RATE = 24_000;
/** 20 ms at 16 kHz = 320 samples = 640 bytes: the frame size the server expects. */
export const FRAME_SAMPLES = 320;

/** Floats in [-1, 1] → little-endian 16-bit, clamped. */
export function floatTo16(samples: Float32Array): Int16Array {
  const out = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    const v = Math.max(-1, Math.min(1, samples[i]));
    out[i] = v < 0 ? Math.round(v * 0x8000) : Math.round(v * 0x7fff);
  }
  return out;
}

/** Little-endian 16-bit bytes → floats in [-1, 1). An odd trailing byte is ignored. */
export function pcm16ToFloat(buf: ArrayBuffer): Float32Array<ArrayBuffer> {
  const n = Math.floor(buf.byteLength / 2);
  const view = new DataView(buf, 0, n * 2);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = view.getInt16(i * 2, true) / 0x8000;
  return out;
}

/** Root-mean-square of a block, for the level pips (0–1). */
export function rms(samples: Float32Array): number {
  if (!samples.length) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return Math.sqrt(sum / samples.length);
}

/** Resamples a float stream from the device rate to 16 kHz and cuts it into 640-byte frames.
 *  Linear interpolation, with the fractional read position carried across blocks so the frames
 *  are continuous. */
export class Framer {
  private readonly step: number;
  private pos = 0; // fractional read position into `pending`
  private pending = new Float32Array(0);
  private out: number[] = [];

  constructor(inRate: number, outRate = IN_RATE) {
    if (!(inRate > 0) || !(outRate > 0)) throw new Error("bad rate");
    this.step = inRate / outRate;
  }

  /** Add a block of device-rate samples; returns the whole frames now ready. */
  push(block: Float32Array): ArrayBuffer[] {
    const joined = new Float32Array(this.pending.length + block.length);
    joined.set(this.pending, 0);
    joined.set(block, this.pending.length);
    let p = this.pos;
    while (p + 1 < joined.length) {
      const i = Math.floor(p);
      const f = p - i;
      this.out.push(joined[i] + (joined[i + 1] - joined[i]) * f);
      p += this.step;
    }
    // The next read may already be past this block's end (a step > 1): carry the overshoot, or
    // every later sample shifts by one.
    const keep = Math.min(Math.floor(p), joined.length);
    this.pending = joined.slice(keep);
    this.pos = p - keep;
    const frames: ArrayBuffer[] = [];
    while (this.out.length >= FRAME_SAMPLES) {
      const chunk = Float32Array.from(this.out.splice(0, FRAME_SAMPLES));
      frames.push(floatTo16(chunk).buffer as ArrayBuffer);
    }
    return frames;
  }
}
