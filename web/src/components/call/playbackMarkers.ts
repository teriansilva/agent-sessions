/** Where HQ's speech has PLAYED to (#1397 P2). The server sends `{"t":"said", id}` in order behind a
 *  say's audio; the browser puts a marker at the end of everything it has queued so far and acks
 *  `{"t":"played", id}` once the audio clock passes it. That ack is what arms a spoken
 *  confirmation, so it must be exact:
 *
 *  - a marker is due only when the clock has reached the end of the audio queued before it;
 *  - an interrupt flushes the queue, and every pending marker with it: a flushed say never acks;
 *  - a clock that does not move (a suspended context, a closed one) never acks. */
export class PlaybackMarkers {
  private pending: { id: string; at: number }[] = [];

  /** A marker at `at` on the audio clock (seconds). Markers keep their order. */
  add(id: string, at: number): void {
    const last = this.pending.at(-1);
    this.pending.push({ id, at: last ? Math.max(at, last.at) : at });
  }

  /** The markers the clock has reached, in order, removed from the queue. */
  due(now: number): string[] {
    const out: string[] = [];
    while (this.pending.length && this.pending[0].at <= now) out.push(this.pending.shift()!.id);
    return out;
  }

  /** Drop every pending marker: none of them will ever ack. */
  flush(): void {
    this.pending = [];
  }

  get size(): number {
    return this.pending.length;
  }
}
