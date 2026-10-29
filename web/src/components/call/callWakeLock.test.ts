/** The screen stays on during a connected call: asked for while visible, asked for again after the
 *  browser released it for a hidden page, and let go of at the end — even mid-request. */
import { expect, test } from "vitest";

import { holdScreen } from "./callWakeLock";

class FakeSentinel {
  released = false;
  release(): Promise<void> {
    this.released = true;
    return Promise.resolve();
  }
}

function fakes(opts: { refuse?: boolean } = {}) {
  const asked: Array<{ grant: (s: FakeSentinel) => void; refuse: (e: unknown) => void }> = [];
  const sentinels: FakeSentinel[] = [];
  const listeners = new Set<() => void>();
  const doc = {
    visibilityState: "visible" as DocumentVisibilityState,
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
  };
  const nav = {
    wakeLock: {
      request: (type: string) => {
        expect(type).toBe("screen");
        if (opts.refuse) return Promise.reject(new DOMException("battery saver", "NotAllowedError"));
        return new Promise<FakeSentinel>((grant, refuse) => asked.push({ grant, refuse }));
      },
    },
  };
  /** Answer the oldest pending request with a new lock. */
  const grant = async (opts: { released?: boolean } = {}) => {
    const s = new FakeSentinel();
    s.released = !!opts.released;
    sentinels.push(s);
    asked.shift()!.grant(s);
    await Promise.resolve();
    await Promise.resolve();
    return s;
  };
  /** Refuse the oldest pending request. */
  const refuse = async () => {
    asked.shift()!.refuse(new DOMException("hidden", "NotAllowedError"));
    await Promise.resolve();
    await Promise.resolve();
  };
  const setVisible = (on: boolean) => {
    doc.visibilityState = on ? "visible" : "hidden";
    for (const fn of [...listeners]) fn();
  };
  return {
    asked,
    sentinels,
    listeners,
    grant,
    refuse,
    setVisible,
    hold: () => holdScreen(nav as unknown as Navigator, doc as unknown as Document),
  };
}

test("a connected call asks for the screen lock, and the end lets go of it", async () => {
  const f = fakes();
  const end = f.hold();
  expect(f.asked).toHaveLength(1);
  const lock = await f.grant();
  end();
  expect(lock.released).toBe(true);
  expect(f.listeners.size).toBe(0);
});

test("after the browser released it for a hidden page, a visible page asks again", async () => {
  const f = fakes();
  const end = f.hold();
  const first = await f.grant();
  // The screen went dark: the page is hidden and the browser lets go of the lock.
  first.released = true;
  f.setVisible(false);
  expect(f.asked).toHaveLength(0); // never asked for while hidden
  f.setVisible(true);
  expect(f.asked).toHaveLength(1);
  const second = await f.grant();
  end();
  expect(second.released).toBe(true);
});

test("a lock still held is not asked for twice", async () => {
  const f = fakes();
  const end = f.hold();
  await f.grant();
  f.setVisible(true);
  expect(f.asked).toHaveLength(0);
  end();
});

test("a lock granted after the call ended is let go of at once", async () => {
  const f = fakes();
  const end = f.hold();
  end();
  const late = await f.grant();
  expect(late.released).toBe(true);
});

test("a refusal, or a browser without the API, changes nothing about the call", async () => {
  const f = fakes({ refuse: true });
  const end = f.hold();
  await Promise.resolve();
  end();
  const none = holdScreen({} as Navigator, document);
  expect(() => none()).not.toThrow();
});

test("hidden and back while a request is pending, then refused: it is asked for again (Hermes 7425)", async () => {
  const f = fakes();
  const end = f.hold();
  expect(f.asked).toHaveLength(1);
  f.setVisible(false);
  f.setVisible(true); // back before the first request settled
  await f.refuse(); // the hidden page invalidated it
  expect(f.asked).toHaveLength(1); // a second request, now that the page is visible
  const lock = await f.grant();
  end();
  expect(lock.released).toBe(true);
});

test("hidden and back while pending, then granted already released: it is asked for again", async () => {
  const f = fakes();
  const end = f.hold();
  f.setVisible(false);
  f.setVisible(true);
  await f.grant({ released: true });
  expect(f.asked).toHaveLength(1);
  const live = await f.grant();
  expect(live.released).toBe(false);
  end();
});

test("a plain refusal is not retried on its own", async () => {
  const f = fakes();
  const end = f.hold();
  await f.refuse();
  expect(f.asked).toHaveLength(0);
  end();
});
