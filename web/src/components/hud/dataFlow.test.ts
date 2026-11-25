import { afterEach, expect, test, vi } from "vitest";

import { runButtonGlitch } from "./dataFlow";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  document.body.innerHTML = "";
  const root = document.documentElement;
  delete root.dataset.motion;
  delete root.dataset.motionPaused;
  delete root.dataset.theme;
  localStorage.removeItem("tr-motion");
});

/** The MutationObserver that carries a live switch delivers on a microtask. */
const flush = () => new Promise((r) => setTimeout(r, 0));

function mockReducedMotion(reduce: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi
      .fn()
      .mockReturnValue({
        matches: reduce,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
  );
}

// The glitch's add/clear + reduced-motion behaviour is pinned through the React wrapper in
// hud.test.tsx; what's new here is the teardown the connect shell relies on when the streamed
// app takes over the screen.
test("runButtonGlitch teardown stops the pending glitch", () => {
  mockReducedMotion(false);
  const rand = vi.spyOn(Math, "random").mockReturnValue(0); // fixed 7000ms schedule
  vi.useFakeTimers();
  document.body.innerHTML = `<button class="shine">Connect</button>`;
  const btn = document.querySelector<HTMLElement>(".shine")!;
  // jsdom has no layout: offsetParent is null, which the "visible only" filter rejects.
  Object.defineProperty(btn, "offsetParent", {
    get: () => document.body,
    configurable: true,
  });

  try {
    runButtonGlitch()();
    vi.advanceTimersByTime(60_000);
    expect(btn.classList.contains("glitching")).toBe(false);
  } finally {
    rand.mockRestore();
  }
});

test("runButtonGlitch arms no timer outside Full, and a live switch arms / clears it (#1531)", async () => {
  mockReducedMotion(false);
  const root = document.documentElement;
  root.dataset.motion = "off";
  const set = vi.spyOn(globalThis, "setTimeout");
  const stop = runButtonGlitch();
  const armed = () => set.mock.calls.filter(([, ms]) => typeof ms === "number" && ms >= 7000).length;
  expect(armed()).toBe(0);

  root.dataset.motion = "full";
  await flush();
  expect(armed()).toBe(1);

  const clear = vi.spyOn(globalThis, "clearTimeout");
  root.dataset.motion = "reduced";
  await flush();
  expect(clear).toHaveBeenCalled();
  stop();
  set.mockRestore();
  clear.mockRestore();
});
