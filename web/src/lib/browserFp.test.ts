import { beforeEach, expect, test } from "vitest";
import { _resetBrowserFpForTests, getBrowserFp, getTabId } from "./browserFp";

beforeEach(() => {
  localStorage.clear();
  _resetBrowserFpForTests();
});

test("getBrowserFp mints a 128-bit hex fp on first call + persists to localStorage", () => {
  const fp = getBrowserFp();
  expect(fp).toMatch(/^[0-9a-f]{32}$/);
  expect(localStorage.getItem("tr-browser-fp")).toBe(fp);
});

test("getBrowserFp is idempotent within one session", () => {
  expect(getBrowserFp()).toBe(getBrowserFp());
});

test("getBrowserFp survives a tab restart by reading from localStorage", () => {
  const original = getBrowserFp();
  _resetBrowserFpForTests();
  // Same localStorage value → same fp returned.
  expect(getBrowserFp()).toBe(original);
});

test("getBrowserFp ignores a corrupt localStorage value", () => {
  localStorage.setItem("tr-browser-fp", "not-a-valid-fp");
  _resetBrowserFpForTests();
  const fresh = getBrowserFp();
  expect(fresh).toMatch(/^[0-9a-f]{32}$/);
  expect(fresh).not.toBe("not-a-valid-fp");
  // The bad value was replaced with the fresh one.
  expect(localStorage.getItem("tr-browser-fp")).toBe(fresh);
});

test("getTabId is stable within one session but distinct across resets", () => {
  const a = getTabId();
  expect(a).toMatch(/^[0-9a-f]{16}$/);
  expect(getTabId()).toBe(a);
  _resetBrowserFpForTests(); // simulates a fresh tab/window
  const b = getTabId();
  expect(b).not.toBe(a);
});

test("getBrowserFp falls back to an in-memory id when localStorage throws", () => {
  const origSet = Storage.prototype.setItem;
  Storage.prototype.setItem = () => {
    throw new Error("quota");
  };
  try {
    _resetBrowserFpForTests();
    const fp = getBrowserFp();
    expect(fp).toMatch(/^[0-9a-f]{32}$/);
    // Same call returns the same in-memory value.
    expect(getBrowserFp()).toBe(fp);
  } finally {
    Storage.prototype.setItem = origSet;
  }
});
