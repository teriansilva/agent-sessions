// Unit tests for the bounded fetched-pages buffer + the ESC[3J purge contract
// (Hermes #365): pagesBuf must be capped with a VISIBLE cap — a page that won't fit is
// REJECTED and the loader latches "capped" (r2 finding 2: the old evict-rewind-refetch
// looped on the same page with no visible progress) — and a server clear-scrollback
// must purge fetched pages so a rewrite can never resurrect cleared content.
import { describe, expect, it } from "vitest";
import type { HistoryPage } from "../types/api";
import { HistoryLoader } from "./historyLoader";
import { PagesBuffer, foldWipe } from "./pagesBuffer";

const page = (ansi: string, cursor: number | null, has_more: boolean): HistoryPage => ({
  ansi,
  cursor,
  has_more,
});

describe("PagesBuffer depth cap", () => {
  it("composes retained pages oldest-first and tracks size", () => {
    const buf = new PagesBuffer(100);
    expect(buf.prepend("newest-history")).toBe(true); // first fetched page
    expect(buf.prepend("older")).toBe(true);
    expect(buf.prepend("oldest")).toBe(true);
    expect(buf.text()).toBe("oldest\r\nolder\r\nnewest-history");
    expect(buf.size).toBe("newest-history".length + "older".length + "oldest".length);
  });

  it("rejects a page that would exceed the cap — retained content is untouched", () => {
    const buf = new PagesBuffer(10);
    expect(buf.prepend("aaaa")).toBe(true); // 4
    expect(buf.prepend("bbbb")).toBe(true); // 8
    expect(buf.prepend("cccc")).toBe(false); // 12 > 10 → REJECTED, not inserted
    // Retained pages unchanged and still contiguous with the live stream — no hole.
    expect(buf.text()).toBe("bbbb\r\naaaa");
    expect(buf.size).toBe(8);
  });

  it("always accepts the FIRST page, even over the cap (never show nothing)", () => {
    const buf = new PagesBuffer(4);
    expect(buf.prepend("bigger-than-cap")).toBe(true);
    expect(buf.text()).toBe("bigger-than-cap");
  });

  it("at the cap, one fetch latches the cap pill and does NOT refetch in a loop", async () => {
    // Hermes #365 r2 finding 2, end-to-end at the unit level: the just-fetched page is
    // refused by the buffer → the loader latches "capped" → further scroll-top events
    // fetch NOTHING (count stays 1) and the retained viewport content never changes.
    let fetches = 0;
    const loader = new HistoryLoader(async () => {
      fetches++;
      return page("deep-page!", 5, true);
    });
    const buf = new PagesBuffer(8);
    buf.prepend("12345678"); // buffer exactly full
    const p = await loader.requestOlder(80); // scroll-top → fetch once
    expect(p).not.toBeNull();
    if (!buf.prepend(p!.ansi)) loader.latchCap(); // Terminal's prependPage logic
    expect(loader.state).toBe("capped"); // → the "— older history beyond local cap —" pill
    // Further scroll-top events while still at the top:
    expect(await loader.requestOlder(80)).toBeNull();
    expect(await loader.requestOlder(80)).toBeNull();
    expect(fetches).toBe(1); // no refetch loop
    expect(buf.text()).toBe("12345678"); // nothing was evicted or replaced
  });

  it("clear() empties the buffer (3J purge)", () => {
    const buf = new PagesBuffer(100);
    buf.prepend("pageA");
    buf.prepend("pageB");
    buf.clear();
    expect(buf.isEmpty).toBe(true);
    expect(buf.text()).toBe("");
    expect(buf.size).toBe(0);
  });
});

describe("foldWipe — server clear-scrollback detection", () => {
  it("passes a wipe-free stream through unchanged", () => {
    expect(foldWipe("hello\r\nworld")).toEqual({ buf: "hello\r\nworld", wiped: false });
  });

  it("keeps only the post-wipe tail behind a plain clear, and reports wiped", () => {
    const r = foldWipe("old stuff\x1b[3Jfresh");
    expect(r.wiped).toBe(true);
    expect(r.buf).toBe("\x1b[H\x1b[2Jfresh");
  });

  it("uses the LAST wipe when there are several", () => {
    const r = foldWipe("a\x1b[3Jb\x1b[3Jc");
    expect(r.buf).toBe("\x1b[H\x1b[2Jc");
    expect(r.wiped).toBe(true);
  });
});

describe("ESC[3J purge end-to-end (pages + loader)", () => {
  it("a wipe clears fetched pages and resets the loader to a fresh first fetch", async () => {
    const fetched: Array<number | undefined> = [];
    const loader = new HistoryLoader(async (q) => {
      fetched.push(q.before);
      return page("old-page", 5, true);
    });
    const buf = new PagesBuffer(1000);
    const p = await loader.requestOlder(80);
    buf.prepend(p!.ansi);

    // The server clears the scrollback → Terminal purges pages + resets the loader.
    const { wiped } = foldWipe("junk\x1b[3Jclean");
    expect(wiped).toBe(true);
    buf.clear();
    loader.reset();

    expect(buf.isEmpty).toBe(true); // nothing left to resurrect on the next rewrite
    const next = await loader.requestOlder(80);
    expect(next?.ansi).toBe("old-page");
    // Cursor was reset → the post-wipe fetch is a fresh FIRST page (no `before`),
    // aligned to the server's post-clear transcript, not the wiped coordinates.
    expect(fetched).toEqual([undefined, undefined]);
  });

  it("a wipe-then-hist reattach reseeds the exact post-clear boundary", async () => {
    // The transcript attach payload leads with ESC[3J, then the {"t":"hist"} frame
    // follows the seq frame: reset() then seed(N) → the next fetch carries before=N.
    const fetched: Array<number | undefined> = [];
    const loader = new HistoryLoader(async (q) => {
      fetched.push(q.before);
      return page("x", 1, true);
    });
    loader.latchCap(); // even a latched cap is superseded by a fresh attach…
    loader.reset(); // …because the wipe resets the machine
    loader.seed(9); // and the hist frame seeds the new exact boundary
    await loader.requestOlder(80);
    expect(fetched).toEqual([9]);
  });

  it("an in-flight fetch from before the reset is discarded, not resurrected", async () => {
    let resolveFetch!: (p: HistoryPage) => void;
    const loader = new HistoryLoader(
      () => new Promise<HistoryPage>((res) => (resolveFetch = res)),
    );
    const pending = loader.requestOlder(80);
    loader.reset(); // 3J lands while the fetch is in flight
    resolveFetch(page("stale-pre-wipe-page", 3, true));
    expect(await pending).toBeNull(); // stale result is dropped → never prepended
    expect(loader.state).toBe("idle"); // and the state machine is back to fresh
  });
});
