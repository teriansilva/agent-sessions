import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TermStatus } from "../../lib/termSocket";
import {
  CONNECT_GRACE_MS,
  LEAVE_MS,
  LOST_GRACE_MS,
  MIN_SHOWN_MS,
  RESTORED_HOLD_MS,
  STABLE_MS,
  VISIBLE_GRACE_MS,
  useUplinkVeil,
} from "./useUplinkVeil";

const connecting: TermStatus = { kind: "connecting" };
const connected: TermStatus = { kind: "connected" };
const retry = (attempt: number): TermStatus => ({ kind: "reconnecting", attempt });

function mount(initial: TermStatus = connecting) {
  return renderHook(({ s }: { s: TermStatus }) => useUplinkVeil(s), {
    initialProps: { s: initial },
  });
}
const advance = (ms: number) => act(() => void vi.advanceTimersByTime(ms));
/** A pane whose link has opened and held (a real, settled connection). */
function mountLive() {
  const h = mount(connected);
  advance(STABLE_MS);
  return h;
}

const setVisibility = (state: "visible" | "hidden") => {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
  act(() => void document.dispatchEvent(new Event("visibilitychange")));
};

beforeEach(() => {
  Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-22T10:00:00Z"));
});
afterEach(() => vi.useRealTimers());

describe("useUplinkVeil — opening / switching to a session (#1570)", () => {
  it("shows nothing when the first connect opens inside the 3 s grace", () => {
    const h = mount();
    advance(CONNECT_GRACE_MS - 100);
    expect(h.result.current).toBeNull();
    h.rerender({ s: connected });
    advance(10_000);
    expect(h.result.current).toBeNull();
  });

  it("announces ESTABLISHING at 3 s, counting tries from 1", () => {
    const h = mount();
    advance(CONNECT_GRACE_MS - 1);
    expect(h.result.current).toBeNull();
    advance(1);
    expect(h.result.current).toMatchObject({ phase: "establishing", attempt: 1, leaving: false });
    h.rerender({ s: retry(1) }); // the first try failed: the socket now runs its second
    expect(h.result.current).toMatchObject({ phase: "establishing", attempt: 2 });
  });

  it("keeps the 3 s clock from the mount across failed first tries", () => {
    const h = mount();
    advance(1000);
    h.rerender({ s: retry(1) });
    advance(CONNECT_GRACE_MS - 1000 - 1);
    expect(h.result.current).toBeNull();
    advance(1);
    expect(h.result.current?.phase).toBe("establishing");
  });
});

describe("useUplinkVeil — a live session loses its link", () => {
  it("shows nothing for a drop that heals inside 5 s", () => {
    const h = mountLive();
    h.rerender({ s: retry(1) });
    advance(LOST_GRACE_MS - 100);
    expect(h.result.current).toBeNull();
    h.rerender({ s: connected });
    advance(10_000);
    expect(h.result.current).toBeNull();
  });

  it("announces RE-ESTABLISHING at 5 s, then RESTORED, then fades out and unmounts", () => {
    const h = mountLive();
    const dropAt = Date.now();
    h.rerender({ s: retry(1) });
    advance(LOST_GRACE_MS);
    expect(h.result.current).toMatchObject({ phase: "lost", since: dropAt, attempt: 1 });
    h.rerender({ s: retry(2) });
    expect(h.result.current).toMatchObject({ phase: "lost", attempt: 2 });
    advance(2000);
    h.rerender({ s: connected });
    advance(STABLE_MS - 1);
    expect(h.result.current?.phase).toBe("lost"); // the link has not held yet
    advance(1);
    expect(h.result.current).toMatchObject({ phase: "restored", leaving: false, downMs: LOST_GRACE_MS + 2000 + STABLE_MS });
    advance(RESTORED_HOLD_MS);
    expect(h.result.current).toMatchObject({ phase: "restored", leaving: true });
    advance(LEAVE_MS);
    expect(h.result.current).toBeNull();
  });

  it("holds an announcement for at least max(600 ms on screen, 750 ms of held link) before RESTORED", () => {
    expect(STABLE_MS).toBeGreaterThanOrEqual(MIN_SHOWN_MS);
    const h = mountLive();
    h.rerender({ s: retry(1) });
    advance(LOST_GRACE_MS);
    advance(100);
    h.rerender({ s: connected });
    advance(STABLE_MS - 1);
    expect(h.result.current?.phase).toBe("lost");
    advance(1);
    expect(h.result.current?.phase).toBe("restored");
  });

  it("a flapping link (opens, closed at once) neither resets the clock nor ends the announcement", () => {
    const h = mountLive();
    h.rerender({ s: retry(1) });
    // Every 1.5 s a retry opens and is refused 50 ms later — the clock keeps running from the drop.
    // TermSocket resets its own counter on every open, so each refusal reads attempt 1 again.
    for (let i = 0; i < 3; i++) {
      advance(1450);
      h.rerender({ s: connected });
      advance(50);
      h.rerender({ s: retry(1) });
    }
    advance(LOST_GRACE_MS - 4500 - 1);
    expect(h.result.current).toBeNull();
    advance(1);
    // The try count kept climbing through the flaps (1 for the drop's retry, +1 per refusal),
    // never reset to the socket's "1".
    expect(h.result.current).toMatchObject({ phase: "lost", attempt: 4 });
    // Once shown, another flap keeps it on screen — RESTORED needs a link that holds.
    h.rerender({ s: connected });
    advance(50);
    h.rerender({ s: retry(1) });
    advance(STABLE_MS * 2);
    expect(h.result.current).toMatchObject({ phase: "lost", attempt: 5 });
    // A link that then holds ends the outage; the next real drop starts counting from 1 again.
    h.rerender({ s: connected });
    advance(STABLE_MS + RESTORED_HOLD_MS + LEAVE_MS);
    expect(h.result.current).toBeNull();
    h.rerender({ s: retry(1) });
    advance(LOST_GRACE_MS);
    expect(h.result.current).toMatchObject({ phase: "lost", attempt: 1 });
  });

  it("a first connect that flaps stays ESTABLISHING on the mount's 3 s clock", () => {
    const h = mount();
    advance(1000);
    h.rerender({ s: connected });
    advance(50);
    h.rerender({ s: retry(1) });
    advance(CONNECT_GRACE_MS - 1050 - 1);
    expect(h.result.current).toBeNull();
    advance(1);
    expect(h.result.current?.phase).toBe("establishing");
  });

  it("starts a fresh 5 s clock for a second drop", () => {
    const h = mountLive();
    h.rerender({ s: retry(1) });
    advance(4000);
    h.rerender({ s: connected });
    advance(20_000);
    h.rerender({ s: retry(1) });
    advance(LOST_GRACE_MS - 1);
    expect(h.result.current).toBeNull();
    advance(1);
    expect(h.result.current?.phase).toBe("lost");
  });
});

describe("useUplinkVeil — never over its own panels, never flashing on tab return", () => {
  it("drops the veil for a deliberate reject and a wrong runtime", () => {
    const h = mount();
    advance(CONNECT_GRACE_MS);
    expect(h.result.current).not.toBeNull();
    h.rerender({ s: { kind: "rejected", reason: "nope", code: 4403 } });
    expect(h.result.current).toBeNull();
    advance(30_000);
    expect(h.result.current).toBeNull();

    const w = mount();
    advance(CONNECT_GRACE_MS);
    w.rerender({ s: { kind: "wrong-runtime" } });
    expect(w.result.current).toBeNull();
  });

  it("waits 1 s after the tab becomes visible even when the outage is older than the grace", () => {
    const h = mountLive();
    h.rerender({ s: retry(1) });
    advance(LOST_GRACE_MS - 200);
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    act(() => void document.dispatchEvent(new Event("visibilitychange")));
    advance(200);
    expect(h.result.current).toBeNull(); // the 5 s are up, but the tab only just came back
    advance(VISIBLE_GRACE_MS - 200 - 1);
    expect(h.result.current).toBeNull();
    advance(1);
    expect(h.result.current?.phase).toBe("lost");
  });

  it("never mounts behind a hidden tab; on return a held outage waits out the 1 s grace (PR #1582 review)", () => {
    const h = mountLive();
    setVisibility("hidden");
    h.rerender({ s: retry(1) });
    advance(6000); // the 5 s grace ran out in the background
    expect(h.result.current).toBeNull();
    setVisibility("visible");
    expect(h.result.current).toBeNull(); // not already on screen at the instant of return
    advance(VISIBLE_GRACE_MS - 1);
    expect(h.result.current).toBeNull();
    advance(1);
    expect(h.result.current).toMatchObject({ phase: "lost" });
  });

  it("a hidden tab whose link comes back right after the return never shows the veil", () => {
    const h = mountLive();
    setVisibility("hidden");
    h.rerender({ s: retry(1) });
    advance(6000);
    setVisibility("visible");
    advance(400); // the socket's own wake reconnects inside the grace
    h.rerender({ s: connected });
    advance(10_000);
    expect(h.result.current).toBeNull();
  });

  it("an outage already on screen comes down while hidden and gets the 1 s grace on return (PR #1582 review 2)", () => {
    const h = mountLive();
    const dropAt = Date.now();
    h.rerender({ s: retry(1) });
    advance(LOST_GRACE_MS);
    h.rerender({ s: retry(2) });
    expect(h.result.current).toMatchObject({ phase: "lost", since: dropAt, attempt: 2 });
    setVisibility("hidden");
    expect(h.result.current).toBeNull();
    h.rerender({ s: retry(3) }); // retries go on in the background
    advance(4000);
    expect(h.result.current).toBeNull();
    setVisibility("visible");
    expect(h.result.current).toBeNull(); // not on screen at the instant of return
    advance(VISIBLE_GRACE_MS - 1);
    expect(h.result.current).toBeNull();
    advance(1);
    // The same outage: its clock and its try count were kept across the pause.
    expect(h.result.current).toMatchObject({ phase: "lost", since: dropAt, attempt: 3 });
  });

  it("an outage on screen when the tab hid, reconnecting inside the return grace, never flashes back", () => {
    const h = mountLive();
    h.rerender({ s: retry(1) });
    advance(LOST_GRACE_MS);
    expect(h.result.current?.phase).toBe("lost");
    setVisibility("hidden");
    advance(3000);
    setVisibility("visible");
    expect(h.result.current).toBeNull();
    advance(300);
    h.rerender({ s: connected });
    // No RESTORED flourish either: nothing was on screen since the return.
    for (const step of [STABLE_MS + 10, RESTORED_HOLD_MS, LEAVE_MS, 10_000]) {
      advance(step);
      expect(h.result.current).toBeNull();
    }
  });

  it("hidden again inside the return grace: nothing mounts while hidden; the next return gets a full grace (PR #1582 review 3)", () => {
    const h = mountLive();
    const dropAt = Date.now();
    h.rerender({ s: retry(1) });
    advance(LOST_GRACE_MS);
    expect(h.result.current?.phase).toBe("lost");
    setVisibility("hidden");
    advance(2000);
    setVisibility("visible");
    advance(500); // inside the return grace…
    setVisibility("hidden"); // …the tab hides again
    advance(3000); // the first return's timer has long since fired
    expect(h.result.current).toBeNull(); // nothing behind the hidden tab
    setVisibility("visible");
    expect(h.result.current).toBeNull(); // nothing at the instant of the second return
    advance(VISIBLE_GRACE_MS - 1);
    expect(h.result.current).toBeNull();
    advance(1);
    expect(h.result.current).toMatchObject({ phase: "lost", since: dropAt });
  });

  it("hidden again inside the return grace, then the link recovers during the last grace: nothing shows", () => {
    const h = mountLive();
    h.rerender({ s: retry(1) });
    advance(LOST_GRACE_MS);
    setVisibility("hidden");
    advance(2000);
    setVisibility("visible");
    advance(500);
    setVisibility("hidden");
    advance(3000);
    setVisibility("visible");
    advance(400);
    h.rerender({ s: connected });
    for (const step of [STABLE_MS + 10, RESTORED_HOLD_MS, LEAVE_MS, 10_000]) {
      advance(step);
      expect(h.result.current).toBeNull();
    }
  });

  it("a fast second hide (back before the first return's timer fires) still gets a full grace (PR #1582 review 4)", () => {
    const h = mountLive();
    h.rerender({ s: retry(1) });
    advance(LOST_GRACE_MS);
    expect(h.result.current?.phase).toBe("lost");
    setVisibility("hidden");
    advance(2000);
    setVisibility("visible");
    advance(400);
    setVisibility("hidden");
    advance(200); // back BEFORE the first return's 1 s timer has fired
    setVisibility("visible");
    advance(VISIBLE_GRACE_MS - 1); // the first return's deadline passes inside this window
    expect(h.result.current).toBeNull();
    advance(1);
    expect(h.result.current?.phase).toBe("lost");
  });

  it("invariant under random tab switches, drops and reconnects: an outage only ever appears on a visible tab, ≥ 1 s after its latest return, and never sits behind a hidden tab", () => {
    // A seeded generator, so a failure names a reproducible sequence.
    for (let seed = 1; seed <= 250; seed++) {
      let x = seed * 2654435761;
      const rnd = () => {
        x = (x ^ (x >>> 15)) * 2246822519;
        x = (x ^ (x >>> 13)) * 3266489917;
        x ^= x >>> 16;
        return (x >>> 0) / 4294967296;
      };
      let visible = true;
      let lastReturn = -Infinity;
      const appeared: { at: number; visible: boolean; sinceReturn: number }[] = [];
      let prevShown = false;
      const h = renderHook(
        ({ s }: { s: TermStatus }) => {
          const v = useUplinkVeil(s);
          const shown = !!v && v.phase !== "restored";
          if (shown && !prevShown)
            appeared.push({ at: Date.now(), visible, sinceReturn: Date.now() - lastReturn });
          prevShown = shown;
          return v;
        },
        { initialProps: { s: connected as TermStatus } },
      );
      let tries = 0;
      const log: string[] = [];
      for (let step = 0; step < 60; step++) {
        const r = rnd();
        if (r < 0.3) {
          visible = !visible;
          if (visible) lastReturn = Date.now();
          log.push(visible ? "show" : "hide");
          setVisibility(visible ? "visible" : "hidden");
          if (!visible) {
            const v = h.result.current;
            expect(!v || v.phase === "restored", `seed ${seed}: ${log.join(",")}`).toBe(true);
          }
        } else if (r < 0.45) {
          tries += 1;
          log.push("drop");
          h.rerender({ s: retry(tries % 3 === 0 ? 1 : tries) }); // includes TermSocket's reset "1"
        } else if (r < 0.6) {
          log.push("open");
          h.rerender({ s: connected });
        } else {
          // Mostly short steps (inside the 1 s grace, where toggles race timers), sometimes long ones.
          const ms = rnd() < 0.7 ? Math.floor(rnd() * 900) : Math.floor(rnd() * 7000);
          log.push(`+${ms}`);
          advance(ms);
        }
      }
      for (const a of appeared) {
        expect(a.visible, `seed ${seed} at ${a.at}: appeared behind a hidden tab — ${log.join(",")}`).toBe(true);
        expect(a.sinceReturn, `seed ${seed}: appeared ${a.sinceReturn} ms after a return — ${log.join(",")}`).toBeGreaterThanOrEqual(VISIBLE_GRACE_MS);
      }
      h.unmount();
      setVisibility("visible");
    }
    // ~1.4 s here; the shared CI runner under load ran the first, larger version past vitest's
    // default 5 s, so the budget is explicit (250 seeds still catch all four reviewed regressions).
  }, 30_000);

  it("clears its timer on unmount", () => {
    const h = mount();
    h.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});
