/** The call's noise gate (#1667): near-silence never reaches the worker's voice detector.
 *
 *  It runs on the 20 ms, 16 kHz PCM frames `Framer` cuts (so its timing is in frames, the same at
 *  any device rate) and decides by a vote of its last 100 ms against ONE fixed level — never an
 *  estimate of the room, never an average a single frame can swing:
 *
 *  - It is **open while at least 3 of the last 5 frames** (100 ms) are at or over -55 dBFS, and
 *    stays open 1 s after that vote fails. So a single click (one or two frames) never opens it;
 *    a word does within 60 ms, its start kept by the 200 ms pre-roll;
 *    exact zeros between words (hard noise suppression) are just frames under the level and
 *    never drag a word down; and a dip under the level shorter than the hold is still heard.
 *    Under that level a room is near-silence — exactly where Whisper invents "Thank you." — and
 *    it goes out as silence. A room 6 dB under it never wins the vote, a click in it included
 *    (closer, a click plus two jittery room frames can, about once in a thousand clicks at 5 dB
 *    under). A room right at the level, its jitter crossing it, may flicker the gate open: then
 *    it reaches the worker's VAD, as without a gate. Every rule that estimated the room, kept a separate closing level or
 *    averaged dB cut a voice or let clicks through on some ordinary input (reviews on #1669).
 *  - Anything louder passes, a steady noise included: the worker's Silero VAD, built to reject
 *    stationary noise, judges it, and Whisper's stock phrases are dropped on the server. That
 *    includes TYPING: keystrokes less than ~100 ms apart (most keys click down and up) win the
 *    vote like syllables do, so a typing Commander's keyboard reaches the VAD as without a gate.
 *    A level gate cannot tell a keystroke from a syllable (#1669 review of de62e14a).
 *  - **Sample-exact pre-roll:** while closed, the newest 200 ms of real audio wait in a queue and
 *    each frame leaving it is sent as SILENCE; on opening, the queue goes out as the real audio.
 *    Every captured frame is sent exactly once (audio or silence), the worker's clock keeps its
 *    cadence, no time is duplicated, and a first syllable is never clipped: the 200 ms pre-roll
 *    covers the 100 ms vote. A mute discards what was queued, as muting should.
 *
 *  Pure: no audio device, so the timing is unit-tested. */

/** 20 ms at 16 kHz, PCM16: what `Framer` produces and the server expects. */
const FRAME_BYTES = 640;

export const GATE = {
  /** The one level (dBFS) a frame is loud at… */
  levelDb: -55,
  /** …and the vote: open while at least `votesNeeded` of the last `windowFrames` frames are loud
   *  (3 of 5: a click is one or two frames), held for `holdFrames` after it fails — past the
   *  longest turn-taking pause (0.6 s). */
  windowFrames: 5, // 100 ms
  votesNeeded: 3,
  holdFrames: 50, // 1 s
  /** Pre-roll: real audio held while closed and sent on opening (200 ms). */
  preRollFrames: 10,
};

/** A PCM16 frame's level in dBFS (digital silence = -120). */
export function frameDb(frame: ArrayBuffer): number {
  const n = Math.floor(frame.byteLength / 2);
  if (!n) return -120;
  const v = new DataView(frame, 0, n * 2);
  let sum = 0;
  for (let i = 0; i < n; i++) {
    const s = v.getInt16(i * 2, true) / 0x8000;
    sum += s * s;
  }
  const rms = Math.sqrt(sum / n);
  return rms > 0 ? Math.max(-120, 20 * Math.log10(rms)) : -120;
}

export class NoiseGate {
  private queue: ArrayBuffer[] = [];
  private openNow = false;
  private hold = 0;
  private recent: boolean[] = [];

  /** Whether real audio is passing (for tests and a future indicator). */
  get open(): boolean {
    return this.openNow;
  }

  /** Back to a fresh start: mute, unmute or a new call. */
  reset(): void {
    this.queue = [];
    this.openNow = false;
    this.hold = 0;
    this.recent = [];
  }

  /** One captured frame in; the frames to send now out (audio, silence, or none while the
   *  pre-roll queue fills). */
  push(frame: ArrayBuffer): ArrayBuffer[] {
    this.recent.push(frameDb(frame) >= GATE.levelDb);
    if (this.recent.length > GATE.windowFrames) this.recent.shift();
    const loud = this.recent.filter(Boolean).length >= GATE.votesNeeded;
    if (this.openNow) {
      if (loud) this.hold = GATE.holdFrames;
      else this.hold -= 1;
      if (this.hold > 0) return [frame];
      // Closing: this quiet frame starts the next pre-roll; nothing goes out until it is full.
      this.openNow = false;
      this.queue = [frame];
      return [];
    }

    if (loud) {
      this.openNow = true;
      this.hold = GATE.holdFrames;
      const out = [...this.queue, frame];
      this.queue = [];
      return out;
    }
    this.queue.push(frame);
    if (this.queue.length <= GATE.preRollFrames) return [];
    this.queue.shift(); // its time has passed unheard: silence goes in its place
    return [new ArrayBuffer(FRAME_BYTES)];
  }
}
