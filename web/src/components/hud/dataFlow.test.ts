import { afterEach, expect, test, vi } from "vitest";

import { runButtonGlitch, runDataFlow } from "./dataFlow";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  document.body.innerHTML = "";
});

function mockReducedMotion(reduce: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockReturnValue({ matches: reduce, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  );
}

/** A canvas whose 2d context records nothing but answers every call the loop makes. */
function fakeCanvas(): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  const ctx = {
    fillStyle: "",
    strokeStyle: "" as string | CanvasGradient,
    lineWidth: 0,
    fillRect: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    stroke: vi.fn(),
    createLinearGradient: vi.fn().mockReturnValue({ addColorStop: vi.fn() }),
  };
  canvas.getContext = vi.fn().mockReturnValue(ctx) as unknown as HTMLCanvasElement["getContext"];
  return canvas;
}

test("runDataFlow is a no-op under prefers-reduced-motion", () => {
  mockReducedMotion(true);
  const raf = vi.fn();
  vi.stubGlobal("requestAnimationFrame", raf);
  const canvas = fakeCanvas();

  const stop = runDataFlow(canvas);

  expect(raf).not.toHaveBeenCalled();
  expect(canvas.getContext).not.toHaveBeenCalled();
  expect(() => stop()).not.toThrow();
});

test("runDataFlow bails without throwing when there is no 2d context", () => {
  mockReducedMotion(false);
  const canvas = document.createElement("canvas"); // jsdom: getContext("2d") -> null
  expect(() => runDataFlow(canvas)()).not.toThrow();
});

test("runDataFlow teardown cancels the frame loop and unbinds resize", () => {
  mockReducedMotion(false);
  vi.stubGlobal("requestAnimationFrame", vi.fn().mockReturnValue(7));
  const cancel = vi.fn();
  vi.stubGlobal("cancelAnimationFrame", cancel);
  const removeListener = vi.spyOn(window, "removeEventListener");

  const stop = runDataFlow(fakeCanvas());
  stop();

  expect(cancel).toHaveBeenCalledWith(7);
  expect(removeListener).toHaveBeenCalledWith("resize", expect.any(Function));
});

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
  Object.defineProperty(btn, "offsetParent", { get: () => document.body, configurable: true });

  try {
    runButtonGlitch()();
    vi.advanceTimersByTime(60_000);
    expect(btn.classList.contains("glitching")).toBe(false);
  } finally {
    rand.mockRestore();
  }
});
