/* The mic capture processor for a call (#1397). It only forwards the first input channel's float
 * samples to the page in batches; the page resamples and frames them (pcm.ts), so the conversion
 * stays unit-tested. Plain JS: an AudioWorklet module is loaded as-is (Vite asset). */
class BattleLabCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Float32Array(2048);
    this.n = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) {
      if (this.n + ch.length > this.buf.length) {
        this.port.postMessage(this.buf.slice(0, this.n));
        this.n = 0;
      }
      this.buf.set(ch, this.n);
      this.n += ch.length;
      if (this.n >= 1024) {
        this.port.postMessage(this.buf.slice(0, this.n));
        this.n = 0;
      }
    }
    return true;
  }
}

registerProcessor("battlelab-capture", BattleLabCapture);
