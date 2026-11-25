import { beforeEach, expect, test, vi } from "vitest";
import {
  applyTheme,
  bootPageTheme,
  bootTheme,
  readStoredTheme,
  storeTheme,
  THEME_STORAGE_KEY,
} from "./applyTheme";

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

test("system is stored as the choice and booted as the resolved mode (#1494)", () => {
  storeTheme("system");
  expect(readStoredTheme()).toBe("system");
  // jsdom ships no matchMedia; give spyOn something to wrap.
  if (!window.matchMedia) window.matchMedia = (() => ({})) as never;
  const spy = vi
    .spyOn(window, "matchMedia")
    .mockImplementation(() => ({ matches: false }) as MediaQueryList);
  try {
    bootTheme();
    expect(document.documentElement.dataset.theme).toBe("light");
  } finally {
    spy.mockRestore();
  }
});

/** Pages outside the app (#1596): a stored choice wins; with none, the device decides — unlike the
 *  app, whose default is dark. */
test("bootPageTheme follows the device when nothing is stored, and a stored choice otherwise", () => {
  if (!window.matchMedia) window.matchMedia = (() => ({})) as never;
  const light = vi.spyOn(window, "matchMedia").mockImplementation(() => ({ matches: false }) as MediaQueryList);
  try {
    bootPageTheme();
    expect(document.documentElement.dataset.theme).toBe("light");
    storeTheme("dark");
    bootPageTheme();
    expect(document.documentElement.dataset.theme).toBe("dark");
    storeTheme("system");
    bootPageTheme();
    expect(document.documentElement.dataset.theme).toBe("light");
  } finally {
    light.mockRestore();
  }
});

test("applyTheme points the browser chrome at the new theme's ground (--bg-0, NIGHTJAR #1531)", () => {
  const meta = document.createElement("meta");
  meta.name = "theme-color";
  meta.content = "#0d0e10";
  const tokens = document.createElement("style");
  tokens.textContent =
    ':root[data-theme="dark"] { --bg-0: #0d0e10; } :root[data-theme="light"] { --bg-0: #e7e8ea; }';
  document.head.append(meta, tokens);
  try {
    applyTheme("light");
    expect(meta.content).toBe("#e7e8ea");
    applyTheme("dark");
    expect(meta.content).toBe("#0d0e10");
  } finally {
    meta.remove();
    tokens.remove();
  }
});

test("applyTheme leaves the theme-color alone where the tokens are absent", () => {
  const meta = document.createElement("meta");
  meta.name = "theme-color";
  meta.content = "#0d0e10";
  document.head.append(meta);
  try {
    applyTheme("light");
    expect(meta.content).toBe("#0d0e10");
  } finally {
    meta.remove();
  }
});
