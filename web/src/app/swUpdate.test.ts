import { afterEach, beforeEach, expect, test, vi } from "vitest";

/** swUpdate keeps module-level state (the single-apply guard, the swap latch), so every test
 *  gets a fresh module instance via resetModules + dynamic import. `location.reload` is
 *  stubbed — jsdom's implementation throws. */

const reload = vi.fn();
let realLocation: Location;

beforeEach(() => {
  vi.resetModules();
  vi.useFakeTimers();
  reload.mockReset();
  realLocation = window.location;
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...realLocation, reload },
  });
});

afterEach(() => {
  vi.useRealTimers();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: realLocation,
  });
  // jsdom has no navigator.serviceWorker by default; drop any fake a test installed.
  delete (navigator as { serviceWorker?: unknown }).serviceWorker;
});

const importFresh = async () => await import("./swUpdate");

test("applySWUpdate without a service worker reloads immediately, once (#661)", async () => {
  const { applySWUpdate } = await importFresh();
  applySWUpdate();
  expect(reload).toHaveBeenCalledTimes(1);
  applySWUpdate(); // second tap while applying — guarded
  expect(reload).toHaveBeenCalledTimes(1);
});

test("applySWUpdate reloads on the controller swap and the fallback never double-fires (#661)", async () => {
  const listeners: Record<string, (() => void)[]> = {};
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: {
      controller: {},
      addEventListener: (type: string, cb: () => void) => {
        (listeners[type] ??= []).push(cb);
      },
    },
  });
  const { applySWUpdate } = await importFresh();
  applySWUpdate();
  expect(reload).not.toHaveBeenCalled(); // waits for the swap (or the fallback timer)
  listeners["controllerchange"]?.forEach((cb) => cb()); // new SW took control
  expect(reload).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(10_000); // fallback timer must not reload AGAIN
  expect(reload).toHaveBeenCalledTimes(1);
});

test("applySWUpdate falls back to a single reload when no new SW turns up (#661)", async () => {
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: { controller: {}, addEventListener: () => {} },
  });
  const { applySWUpdate } = await importFresh();
  applySWUpdate();
  expect(reload).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(10_000);
  expect(reload).toHaveBeenCalledTimes(1); // the tap never dead-ends
});

test("initSWUpdates counts only a controller SWAP as an update — not the first claim (#661)", async () => {
  const listeners: Record<string, (() => void)[]> = {};
  // No controller yet: this is a first visit — the initial claim must NOT read as an update.
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: {
      controller: null,
      addEventListener: (type: string, cb: () => void) => {
        (listeners[type] ??= []).push(cb);
      },
    },
  });
  const mod = await importFresh();
  void mod.initSWUpdates(); // virtual:pwa-register is absent in vitest — caught internally
  const seen: boolean[] = [];
  mod.onSWSwap(() => seen.push(true));
  listeners["controllerchange"]?.forEach((cb) => cb()); // first claim after install
  expect(mod.swHasSwapped()).toBe(false);
  expect(seen).toHaveLength(0);
  listeners["controllerchange"]?.forEach((cb) => cb()); // a REAL swap (new SW replaced old)
  expect(mod.swHasSwapped()).toBe(true);
  expect(seen).toHaveLength(1);
});
