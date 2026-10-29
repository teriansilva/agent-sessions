/** When each chunk of HQ's speech plays (#1397): back to back while chunks arrive in time, and a
 *  cushion when the queue has run dry, so network jitter on the way to the browser isn't heard.
 *
 *  - A chunk that arrives while earlier speech is still queued (its end is still ahead of the
 *    audio clock) plays straight after it — however little time is left (Hermes 6984).
 *  - A chunk that finds the queue RUN DRY starts `cushion` ahead. After a real pause (a new
 *    utterance) or a flush, that is the start-up cushion again; after an UNDERRUN mid-utterance
 *    (the queue ran dry less than `pauseS` ago) the cushion grows by `stepS`, up to `maxS`, for
 *    the rest of that utterance: a jittery path buys itself headroom instead of stuttering on
 *    every late chunk, and a clean next turn starts without the extra delay.
 *  - The steps are seconds, not tens of milliseconds: a self-hosted TTS can synthesize SLOWER
 *    than real time (VoxCPM2 measured at 1.05–2.1× on sentence-length text, up to 6 s behind
 *    within one sentence), and no cushion hides that. What the cushion decides is how the silence
 *    is spent: 60 ms steps up to 0.4 s spent it as many short gaps inside words (heard as
 *    stutter); a one-second rebuffer spends it as a pause or two. On recorded replies through the
 *    live relay, that halved the gaps and all but removed the short ones, for 80 ms more before
 *    the first word. A queue dry for under 2 s is still the same reply: synthesis of a slow
 *    sentence can leave a hole that long, and it must not reset what the reply has learned.
 *  Pure: the audio clock is passed in, so the schedule is unit-tested. */

export const PLAYBACK = {
  /** The headroom a fresh utterance starts with… */
  startS: 0.2,
  /** …grown by this much after each mid-utterance underrun (a rebuffer, not a nudge)… */
  stepS: 1.0,
  /** …up to this much. */
  maxS: 3.0,
  /** A queue dry for longer than this was a pause between utterances, not an underrun. */
  pauseS: 2.0,
};

export class PlaybackClock {
  private nextAt = 0;
  private cushionS = PLAYBACK.startS;
  private underrunCount = 0;

  /** The start time for a chunk of `durS` seconds arriving when the audio clock reads `now`. */
  schedule(now: number, durS: number): number {
    let at: number;
    if (this.nextAt > now) {
      at = this.nextAt; // still playing: straight on, no gap
    } else {
      const midUtterance = this.nextAt > 0 && now - this.nextAt < PLAYBACK.pauseS;
      if (midUtterance) {
        this.underrunCount += 1;
        this.cushionS = Math.min(PLAYBACK.maxS, this.cushionS + PLAYBACK.stepS);
      } else {
        this.cushionS = PLAYBACK.startS; // a new utterance starts fresh
      }
      at = now + this.cushionS;
    }
    this.nextAt = at + durS;
    return at;
  }

  /** When everything scheduled so far has played (markers sit here). */
  get end(): number {
    return this.nextAt;
  }

  get cushion(): number {
    return this.cushionS;
  }

  get underruns(): number {
    return this.underrunCount;
  }

  /** A flush (barge-in): nothing is queued any more, and the next speech starts fresh. */
  flush(): void {
    this.nextAt = 0;
    this.cushionS = PLAYBACK.startS;
  }
}
