import { afterEach, describe, expect, test, vi } from "vitest";

import {
  ambientAllowed,
  bootMotion,
  coerceMotionChoice,
  motionMode,
  MOTION_STORAGE_KEY,
  readStoredMotion,
  resolveMotion,
  startMotionPause,
  storeMotion,
} from "./motion";

const root = document.documentElement;

function device(reduce: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: reduce && q.includes("prefers-reduced-motion"),
    media: q,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  })) as unknown as typeof window.matchMedia;
}

afterEach(() => {
  localStorage.removeItem(MOTION_STORAGE_KEY);
  delete root.dataset.motion;
  delete root.dataset.motionPaused;
  // @ts-expect-error -- jsdom has no matchMedia of its own
  delete window.matchMedia;
  vi.useRealTimers();
});

describe("the choice and the mode (#1531, docs/design.md v2 §8)", () => {
  test("Match device follows prefers-reduced-motion; an explicit choice wins", () => {
    expect(resolveMotion("system", false)).toBe("full");
    expect(resolveMotion("system", true)).toBe("reduced");
    expect(resolveMotion("full", true)).toBe("full");
    expect(resolveMotion("off", false)).toBe("off");
    expect(resolveMotion("reduced", false)).toBe("reduced");
  });

  test("an unknown stored value is the default, never a throw", () => {
    expect(coerceMotionChoice("turbo")).toBe("system");
    expect(coerceMotionChoice(null)).toBe("system");
    localStorage.setItem(MOTION_STORAGE_KEY, "nope");
    expect(readStoredMotion()).toBe("system");
  });

  test("storing the default removes the key; storing a choice writes it", () => {
    storeMotion("off");
    expect(localStorage.getItem(MOTION_STORAGE_KEY)).toBe("off");
    storeMotion("system");
    expect(localStorage.getItem(MOTION_STORAGE_KEY)).toBeNull();
  });

  test("bootMotion applies the stored choice against the device", () => {
    device(true);
    bootMotion();
    expect(root.dataset.motion).toBe("reduced");
    localStorage.setItem(MOTION_STORAGE_KEY, "full");
    bootMotion();
    expect(root.dataset.motion).toBe("full");
  });

  test("motionMode reads the attribute first, else resolves the stored choice", () => {
    root.dataset.motion = "off";
    expect(motionMode()).toBe("off");
    delete root.dataset.motion;
    localStorage.setItem(MOTION_STORAGE_KEY, "reduced");
    expect(motionMode()).toBe("reduced");
  });

  test("ambient loops run only in Full and only while nothing paused them", () => {
    root.dataset.motion = "full";
    expect(ambientAllowed()).toBe(true);
    root.dataset.motionPaused = "hidden";
    expect(ambientAllowed()).toBe(false);
    delete root.dataset.motionPaused;
    root.dataset.motion = "reduced";
    expect(ambientAllowed()).toBe(false);
  });
});

describe("the pause (hidden tab, 10 idle minutes)", () => {
  test("idle after the window, cleared by input; the timer is re-armed, not reset per move", () => {
    vi.useFakeTimers();
    const stop = startMotionPause(1000);
    expect(root.dataset.motionPaused).toBeUndefined();
    vi.advanceTimersByTime(600);
    window.dispatchEvent(new Event("pointermove"));
    vi.advanceTimersByTime(600); // 1200 since start, 600 since input: not idle yet
    expect(root.dataset.motionPaused).toBeUndefined();
    vi.advanceTimersByTime(500);
    expect(root.dataset.motionPaused).toBe("idle");
    window.dispatchEvent(new Event("keydown"));
    expect(root.dataset.motionPaused).toBeUndefined();
    stop();
  });

  test("hidden while the document is hidden; teardown clears the attribute", () => {
    const stop = startMotionPause(60_000);
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
    expect(root.dataset.motionPaused).toBe("hidden");
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
    expect(root.dataset.motionPaused).toBeUndefined();
    root.dataset.motionPaused = "idle";
    stop();
    expect(root.dataset.motionPaused).toBeUndefined();
  });
});
