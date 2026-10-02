// Scroll-up lazy-load pagination state machine (#348 Phase 3).
//
// Owns the cursor + single-inflight + end/error/capped states for the paged transcript-
// history endpoint, separate from the xterm wiring in Terminal.tsx so the contract is
// unit-testable:
//  - one request in flight at a time (the server also 429s concurrent renders per session);
//  - the cursor only advances on SUCCESS — an error keeps it, so retry re-issues the same page;
//  - `has_more=false` latches the "end" state (start-of-history pill) permanently;
//  - an error latches "error" until an explicit retry() — further scroll-to-top events
//    must not hammer a failing endpoint;
//  - a PagesBuffer depth-cap latches "capped" (Hermes #365 r2): the just-fetched page could
//    not be retained, so fetching deeper would only refetch-and-discard the same page
//    forever. Terminal until reset() (a server scrollback wipe).
import type { HistoryPage } from "../types/api";

export type HistoryState = "idle" | "loading" | "end" | "error" | "capped";

export type HistoryFetch = (q: {
  before?: number;
  cols: number;
}) => Promise<HistoryPage>;

export class HistoryLoader {
  /** undefined → never seeded (the first fetch omits `before`; the server answers with its
   *  width-INDEPENDENT approximate fallback); number → next-older cursor (seeded from the
   *  server's {"t":"hist"} attach frame, or advanced from a fetched page); null → oldest
   *  reached (mirrors the wire contract). */
  private cursor: number | null | undefined = undefined;
  private inFlight = false;
  private current: HistoryState = "idle";
  /** Bumped by reset(): an in-flight fetch from before the reset must be discarded —
   *  its page would resurrect content the server cleared (ESC[3J). */
  private gen = 0;
  private readonly fetchPage: HistoryFetch;
  private readonly onState: (s: HistoryState) => void;

  constructor(
    fetchPage: HistoryFetch,
    onState: (s: HistoryState) => void = () => {},
  ) {
    this.fetchPage = fetchPage;
    this.onState = onState;
  }

  get state(): HistoryState {
    return this.current;
  }

  private setState(s: HistoryState): void {
    this.current = s;
    this.onState(s);
  }

  /** Seed the exact first-page cursor from the server's {"t":"hist"} attach frame (#348 /
   *  Hermes #365 r2 finding 1): every subsequent fetch then sends `before=` — the exact,
   *  width-stable turn boundary of what the attach delivered — so the server never
   *  re-derives that boundary at a (possibly resized) later width. The frame follows the
   *  transcript attach payload, whose leading ESC[3J already purged pages + reset() this
   *  loader, so seeding lands on a fresh idle machine; latched end/error from a pre-frame
   *  fetch are superseded by the new authoritative boundary. */
  seed(cursor: number): void {
    this.cursor = cursor;
    if (this.current === "end" || this.current === "error")
      this.setState("idle");
  }

  /** Fetch the next older page (the scroll-to-top trigger). Returns the page, or null when
   *  nothing was fetched (already loading / ended / capped / errored-awaiting-retry /
   *  failed / reset mid-flight). */
  async requestOlder(cols: number): Promise<HistoryPage | null> {
    if (
      this.inFlight ||
      this.current === "end" ||
      this.current === "error" ||
      this.current === "capped"
    )
      return null;
    this.inFlight = true;
    const gen = this.gen;
    const before = this.cursor === null ? undefined : this.cursor;
    this.setState("loading");
    try {
      const page = await this.fetchPage({ before, cols });
      if (gen !== this.gen) return null; // reset() raced this fetch → stale, discard
      this.cursor = page.cursor;
      this.setState(page.has_more ? "idle" : "end");
      return page;
    } catch {
      // Cursor NOT advanced → retry re-issues exactly the same page.
      if (gen === this.gen) this.setState("error");
      return null;
    } finally {
      if (gen === this.gen) this.inFlight = false;
    }
  }

  /** Clear a latched error and re-issue the same cursor (the error pill's tap target). */
  retry(cols: number): Promise<HistoryPage | null> {
    if (this.current === "error") this.current = "idle";
    return this.requestOlder(cols);
  }

  /** The PagesBuffer refused the just-fetched page — the local depth cap is reached
   *  (Hermes #365 r2 finding 2). Latch the terminal "capped" state: the cap pill takes
   *  the start-of-history pill's slot and NO further auto-fetches fire (refetching would
   *  evict the same page again — the refetch loop with no visible progress). Only
   *  reset() (a server scrollback wipe) clears it. */
  latchCap(): void {
    this.setState("capped");
  }

  /** Back to the initial state (fresh first-page fetch). Called when the server clears
   *  the scrollback (ESC[3J): the old cursor coordinates — and any in-flight page —
   *  belong to content the server wiped and must not be resurrected. */
  reset(): void {
    this.gen++;
    this.cursor = undefined;
    this.inFlight = false;
    this.setState("idle");
  }
}
