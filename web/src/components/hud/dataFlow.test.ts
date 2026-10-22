import { afterEach, expect, test, vi } from "vitest";

import { PAINT_MS, runButtonGlitch, runDataFlow } from "./dataFlow";

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

/** A canvas whose 2d context records nothing but answers every call the loop makes. */
function fakeCanvas(): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  const ctx = {
    fillStyle: "",
    strokeStyle: "" as string | CanvasGradient,
    lineWidth: 0,
    fillRect: vi.fn(),
    clearRect: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    stroke: vi.fn(),
    createLinearGradient: vi.fn().mockReturnValue({ addColorStop: vi.fn() }),
  };
  canvas.getContext = vi
    .fn()
    .mockReturnValue(ctx) as unknown as HTMLCanvasElement["getContext"];
  return canvas;
}

for (const mode of ["reduced", "off"]) {
  test(`runDataFlow draws a still frame and starts no loop in the ${mode} mode (#1531)`, () => {
    mockReducedMotion(false);
    document.documentElement.dataset.motion = mode;
    const raf = vi.fn();
    vi.stubGlobal("requestAnimationFrame", raf);
    const canvas = fakeCanvas();

    const stop = runDataFlow(canvas);

    expect(raf).not.toHaveBeenCalled();
    const ctx = (canvas.getContext as unknown as ReturnType<typeof vi.fn>).mock.results[0].value;
    expect(ctx.clearRect).toHaveBeenCalled(); // the still grid
    expect(() => stop()).not.toThrow();
  });
}

test("Match device on a reduce-motion device starts no loop (#1531)", () => {
  mockReducedMotion(true);
  const raf = vi.fn();
  vi.stubGlobal("requestAnimationFrame", raf);
  runDataFlow(fakeCanvas())();
  expect(raf).not.toHaveBeenCalled();
});

test("a live switch Full → Off → Full stops the loop and starts it again (#1531)", async () => {
  mockReducedMotion(false);
  const root = document.documentElement;
  root.dataset.motion = "full";
  const raf = vi.fn().mockReturnValue(11);
  const cancel = vi.fn();
  vi.stubGlobal("requestAnimationFrame", raf);
  vi.stubGlobal("cancelAnimationFrame", cancel);

  const stop = runDataFlow(fakeCanvas());
  expect(raf).toHaveBeenCalledTimes(1);

  root.dataset.motion = "off";
  await flush();
  expect(cancel).toHaveBeenCalledWith(11);

  raf.mockClear();
  root.dataset.motion = "full";
  await flush();
  expect(raf).toHaveBeenCalledTimes(1);
  stop();
});

test("a hidden-tab / idle pause and the light theme stop the loop (#1531)", async () => {
  mockReducedMotion(false);
  const root = document.documentElement;
  root.dataset.motion = "full";
  const raf = vi.fn().mockReturnValue(5);
  const cancel = vi.fn();
  vi.stubGlobal("requestAnimationFrame", raf);
  vi.stubGlobal("cancelAnimationFrame", cancel);
  const stop = runDataFlow(fakeCanvas());

  root.dataset.motionPaused = "idle";
  await flush();
  expect(cancel).toHaveBeenCalledTimes(1);
  delete root.dataset.motionPaused;
  await flush();
  root.dataset.theme = "light";
  await flush();
  expect(cancel).toHaveBeenCalledTimes(2);
  stop();
});

test("runDataFlow bails without throwing when there is no 2d context", () => {
  mockReducedMotion(false);
  const canvas = document.createElement("canvas"); // jsdom: getContext("2d") -> null
  expect(() => runDataFlow(canvas)()).not.toThrow();
});

test("runDataFlow teardown cancels the frame loop and unbinds resize", () => {
  mockReducedMotion(false);
  document.documentElement.dataset.motion = "full";
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

test("between two paints the loop waits on a timer and asks for no animation frame (#1531)", () => {
  mockReducedMotion(false);
  document.documentElement.dataset.motion = "full";
  vi.useFakeTimers();
  let pending: FrameRequestCallback | null = null;
  const raf = vi.fn((cb: FrameRequestCallback) => {
    pending = cb;
    return 3;
  });
  vi.stubGlobal("requestAnimationFrame", raf);
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  const canvas = fakeCanvas();
  const ctx = canvas.getContext("2d") as unknown as { fillRect: ReturnType<typeof vi.fn> };
  const stop = runDataFlow(canvas);
  expect(raf).toHaveBeenCalledTimes(1);

  // The first frame paints, then the loop sleeps PAINT_MS without asking for frames: each frame it
  // asked for would be a main-thread frame (style pass included) whether it painted or not.
  const before = ctx.fillRect.mock.calls.length;
  pending!(1000);
  expect(ctx.fillRect.mock.calls.length).toBeGreaterThan(before);
  expect(raf).toHaveBeenCalledTimes(1);
  vi.advanceTimersByTime(PAINT_MS - 1);
  expect(raf).toHaveBeenCalledTimes(1);
  vi.advanceTimersByTime(1);
  expect(raf).toHaveBeenCalledTimes(2);

  // Stopping clears the pending wait too: no frame is asked for after teardown.
  pending!(1000 + PAINT_MS);
  stop();
  vi.advanceTimersByTime(PAINT_MS * 4);
  expect(raf).toHaveBeenCalledTimes(2);
});
