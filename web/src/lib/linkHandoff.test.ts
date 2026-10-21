import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { classifyLink } from "./linkEntry";
import { acceptLinks, offerLink } from "./linkHandoff";

// A minimal Web Locks stand-in: an exclusive name goes to the first request and `ifAvailable`
// losers get null; shared holds are counted until their callback's promise settles.
/** The next exclusive request waits this long before it is decided — a tab whose main thread is
 *  busy when the claim arrives. */
let slowNext = 0;

function installLocks() {
  const held = new Set<string>();
  const shared = new Map<string, number>();
  Object.defineProperty(navigator, "locks", {
    configurable: true,
    value: {
      query: async () => ({
        held: [...held, ...[...shared].filter(([, n]) => n > 0).map(([k]) => k)].map((name) => ({
          name,
        })),
      }),
      request: async (
        name: string,
        opts: { ifAvailable?: boolean; mode?: string },
        cb: (lock: object | null) => Promise<unknown>,
      ) => {
        if (opts.mode === "shared") {
          shared.set(name, (shared.get(name) ?? 0) + 1);
          try {
            return await cb({ name });
          } finally {
            shared.set(name, shared.get(name)! - 1);
          }
        }
        if (slowNext) {
          const ms = slowNext;
          slowNext = 0;
          await new Promise((r) => setTimeout(r, ms));
        }
        if (held.has(name)) return cb(null);
        held.add(name);
        return cb({ name });
      },
    },
  });
}

const entry = classifyLink("/s/claude/abc", window.location.origin)!;
const stops: (() => void)[] = [];

beforeEach(installLocks);
afterEach(() => {
  stops.splice(0).forEach((s) => s());
  vi.useRealTimers();
});

describe("tab hand-off (#1232)", () => {
  it("with no other tab, the link stays here — without waiting", async () => {
    const t0 = performance.now();
    expect(await offerLink(entry, 5000)).toBe(false);
    expect(performance.now() - t0).toBeLessThan(1000);
  });

  it("a tab that stopped taking links no longer counts as open", async () => {
    acceptLinks(vi.fn())();
    await new Promise((r) => setTimeout(r, 0));
    const t0 = performance.now();
    expect(await offerLink(entry, 5000)).toBe(false);
    expect(performance.now() - t0).toBeLessThan(1000);
  });

  it("exactly one existing tab takes the link and opens it", async () => {
    const a = vi.fn();
    const b = vi.fn();
    stops.push(acceptLinks(a), acceptLinks(b));
    expect(await offerLink(entry, 500)).toBe(true);
    await new Promise((r) => setTimeout(r, 20));
    expect(a.mock.calls.length + b.mock.calls.length).toBe(1);
    expect((a.mock.calls[0] ?? b.mock.calls[0])[0]).toEqual(entry);
  });

  it("a tab that answers after the wait stands down — the link never opens twice", async () => {
    const open = vi.fn();
    stops.push(acceptLinks(open));
    // The existing tab gets to the claim 200 ms late, well past the 30 ms wait.
    slowNext = 200;
    expect(await offerLink(entry, 30)).toBe(false);
    await new Promise((r) => setTimeout(r, 300));
    expect(open).not.toHaveBeenCalled();
  });

  it("a receiver drops a claim that does not classify", async () => {
    const open = vi.fn();
    stops.push(acceptLinks(open));
    const ch = new BroadcastChannel("battlelab-links");
    ch.postMessage({ t: "claim", id: "x", path: "https://evil.example/s/claude/abc" });
    ch.postMessage({ t: "claim", id: "y", path: "/settings" });
    await new Promise((r) => setTimeout(r, 50));
    ch.close();
    expect(open).not.toHaveBeenCalled();
  });

  it("without Web Locks nobody takes it, and the link stays here", async () => {
    Object.defineProperty(navigator, "locks", { configurable: true, value: undefined });
    const open = vi.fn();
    stops.push(acceptLinks(open));
    expect(await offerLink(entry, 50)).toBe(false);
    expect(open).not.toHaveBeenCalled();
  });
});
