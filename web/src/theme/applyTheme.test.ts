import { beforeEach, expect, test } from "vitest";
import { applyTheme, bootTheme, readStoredTheme, storeTheme, THEME_STORAGE_KEY } from "./applyTheme";

beforeEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

test("readStoredTheme returns the default when nothing is stored", () => {
  expect(readStoredTheme()).toBe("dark");
});

test("readStoredTheme coerces an unknown stored value to the default", () => {
  localStorage.setItem(THEME_STORAGE_KEY, "neon");
  expect(readStoredTheme()).toBe("dark");
});

test("legacy `royal` migrates to dark on read (#211)", () => {
  localStorage.setItem(THEME_STORAGE_KEY, "royal");
  expect(readStoredTheme()).toBe("dark");
});

test("store + read round-trips a valid theme", () => {
  storeTheme("dark");
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  expect(readStoredTheme()).toBe("dark");
});

test("applyTheme sets <html data-theme>", () => {
  applyTheme("light");
  expect(document.documentElement.dataset.theme).toBe("light");
});

test("bootTheme applies the device-cached theme", () => {
  storeTheme("dark");
  bootTheme();
  expect(document.documentElement.dataset.theme).toBe("dark");
});
