// Client-side buffer of fetched scroll-up history pages (#348 Phase 3).
//
// xterm.js can't prepend into an existing buffer, so fetched older pages are kept here
// and the whole terminal is re-written on each prepend (see Terminal.tsx). Two hard
// rules (Hermes #365):
//
//  - BOUNDED, with a VISIBLE cap (r2 finding 2): the retained text is capped. A newly
//    fetched page is always the DEEPEST content (index 0), so "evicting to make room"
//    would drop the just-fetched page itself — and shedding the shallow end instead is
//    impossible, because those pages must stay contiguous with the live stream (a rewrite
//    must never show a hole between pages and stream). The earlier rewind-and-discard
//    therefore looped: same page refetched, evicted, refetched — no visible progress. So
//    at the cap `prepend` REJECTS the page outright and the caller latches the loader
//    into its terminal "capped" state (the "— older history beyond local cap —" pill);
//    no further auto-fetches until a server wipe resets everything.
//
//  - A server clear-scrollback (ESC[3J) PURGES everything: replaying previously fetched
//    pages after the server explicitly cleared the scrollback would resurrect content
//    the user/agent wiped. `foldWipe` detects + normalizes the wipe in the recorded
//    stream; on `wiped` the caller clears this buffer and resets the loader.

export class PagesBuffer {
  private pages: string[] = []; // index 0 = deepest / oldest content
  private total = 0;
  private readonly cap: number;

  constructor(cap: number) {
    this.cap = cap;
  }

  get size(): number {
    return this.total;
  }

  get isEmpty(): boolean {
    return this.pages.length === 0;
  }

  /** Retained pages oldest-first, CRLF-joined — the rewrite's prefix block. */
  text(): string {
    return this.pages.join("\r\n");
  }

  /** Prepend an older page (it becomes the new deepest content). Returns false when
   *  retaining it would exceed the cap — the page is NOT inserted and the caller latches
   *  the depth cap (see the header). The FIRST page is always accepted: the server's
   *  per-page render caps keep one page far below any sane buffer cap, and showing
   *  nothing at all would be strictly worse than overshooting once. */
  prepend(ansi: string): boolean {
    if (this.pages.length > 0 && this.total + ansi.length > this.cap) return false;
    this.pages.unshift(ansi);
    this.total += ansi.length;
    return true;
  }

  /** Server cleared the scrollback (ESC[3J): drop everything — history must not
   *  resurrect what the server cleared. */
  clear(): void {
    this.pages = [];
    this.total = 0;
  }
}

/** Detect + normalize a server clear-scrollback in the recorded stream. Everything
 *  before the LAST ESC[3J is no longer on the user's screen; keeping only the post-wipe
 *  tail (behind a plain home+clear) makes a rewrite reproduce what is actually visible.
 *  Returns `wiped` so the caller can also purge fetched pages + reset the loader. */
export function foldWipe(streamBuf: string): { buf: string; wiped: boolean } {
  const wipe = streamBuf.lastIndexOf("\x1b[3J");
  if (wipe < 0) return { buf: streamBuf, wiped: false };
  return { buf: "\x1b[H\x1b[2J" + streamBuf.slice(wipe + 4), wiped: true };
}
