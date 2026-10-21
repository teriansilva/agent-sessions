import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, describe, expect, test } from "vitest";
import {
  coerceTermFontSize,
  DEFAULT_TERM_FONT_SIZE,
  hasStoredTermFontSize,
  isTermFontSize,
  readStoredTermFontSize,
  stepTermFontSize,
  storeTermFontSize,
  TERM_FONT_SIZE_MAX,
  TERM_FONT_SIZE_MIN,
  TERM_SIZE_STORAGE_KEY,
} from "./termSize";

// The SAME fixture tests/test_prefs.py reads (#859). Loaded through node:fs rather than a
// JSON import so nothing depends on the bundler resolving a path outside web/. One list in
// one file is the whole point: these are the cases where two hand-written suites drift.
// Resolved from the vitest root (web/), not import.meta.url: under vitest's transform
// import.meta.url is not a file: URL, so `new URL(...)` throws before a single case runs.
const FIXTURE = resolve(process.cwd(), "../tests/fixtures/term_font_size_cases.json");
const CASES = JSON.parse(readFileSync(FIXTURE, "utf8")) as {
  min: number;
  max: number;
  default: number;
  read: { in: unknown; out: number; why: string }[];
  write_accepted: number[];
  write_rejected: unknown[];
};

describe("termSize normalization", () => {
  test("the shared fixture describes THIS module's constants", () => {
    // Widening the range on one side only would leave the shared table describing neither
    // implementation. Fail here, loudly, rather than in a confusing per-case assertion.
    expect(CASES.min).toBe(TERM_FONT_SIZE_MIN);
    expect(CASES.max).toBe(TERM_FONT_SIZE_MAX);
    expect(CASES.default).toBe(DEFAULT_TERM_FONT_SIZE);
  });

  test("the shared read table", () => {
    for (const c of CASES.read) {
      expect(coerceTermFontSize(c.in), `${JSON.stringify(c.in)}: ${c.why}`).toBe(c.out);
    }
  });

  test("half values round AWAY FROM ZERO, not the way Math.round vs round() disagree", () => {
    // The specific divergence this pins: Python's round(10.5) is 10 (banker's) while
    // Math.round(10.5) is 11. Both sides spell floor(x + 0.5) so they agree by construction.
    expect(coerceTermFontSize(10.5)).toBe(11);
    expect(coerceTermFontSize(12.5)).toBe(13);
  });

  test("booleans are not numbers, and false must not read as 0-then-clamp", () => {
    expect(coerceTermFontSize(true)).toBe(DEFAULT_TERM_FONT_SIZE);
    expect(coerceTermFontSize(false)).toBe(DEFAULT_TERM_FONT_SIZE);
    expect(coerceTermFontSize(false)).not.toBe(TERM_FONT_SIZE_MIN);
  });

  test("JS-only non-finite edges (JSON cannot carry these)", () => {
    expect(coerceTermFontSize(NaN)).toBe(DEFAULT_TERM_FONT_SIZE);
    expect(coerceTermFontSize(Infinity)).toBe(DEFAULT_TERM_FONT_SIZE);
    expect(coerceTermFontSize(-Infinity)).toBe(DEFAULT_TERM_FONT_SIZE);
    expect(coerceTermFontSize(undefined)).toBe(DEFAULT_TERM_FONT_SIZE);
  });

  test("isTermFontSize accepts only canonical values", () => {
    for (const v of CASES.write_accepted) expect(isTermFontSize(v)).toBe(true);
    for (const v of CASES.write_rejected) expect(isTermFontSize(v)).toBe(false);
    expect(isTermFontSize(12.5)).toBe(false);
  });

  test("stepTermFontSize clamps at both ends", () => {
    expect(stepTermFontSize(13, -1)).toBe(12);
    expect(stepTermFontSize(13, 1)).toBe(14);
    expect(stepTermFontSize(TERM_FONT_SIZE_MIN, -1)).toBe(TERM_FONT_SIZE_MIN);
    expect(stepTermFontSize(TERM_FONT_SIZE_MAX, 1)).toBe(TERM_FONT_SIZE_MAX);
    // A step that can't move is how the UI knows to disable the button.
    expect(stepTermFontSize(TERM_FONT_SIZE_MIN, -1)).toBe(TERM_FONT_SIZE_MIN);
  });
});

describe("termSize storage", () => {
  beforeEach(() => localStorage.clear());

  test("storage is string-typed, so the read parses before coercing", () => {
    // The trap: getItem returns "10", and coerceTermFontSize (correctly) rejects a string.
    // The parse belongs at the storage boundary, not inside the shared coercion.
    localStorage.setItem(TERM_SIZE_STORAGE_KEY, "10");
    expect(readStoredTermFontSize()).toBe(10);
    expect(hasStoredTermFontSize()).toBe(true);
  });

  test("an edited cache cannot strand the terminal", () => {
    localStorage.setItem(TERM_SIZE_STORAGE_KEY, "2");
    expect(readStoredTermFontSize()).toBe(TERM_FONT_SIZE_MIN);
    localStorage.setItem(TERM_SIZE_STORAGE_KEY, "banana");
    expect(readStoredTermFontSize()).toBe(DEFAULT_TERM_FONT_SIZE);
  });

  test("an out-of-range cache is not an explicit choice", () => {
    // hasStored gates the one-time server seed. A clamped value is usable but was never
    // *chosen*, so it must not out-rank the server's — otherwise a corrupt cache would
    // permanently pin the device.
    localStorage.setItem(TERM_SIZE_STORAGE_KEY, "2");
    expect(hasStoredTermFontSize()).toBe(false);
    localStorage.setItem(TERM_SIZE_STORAGE_KEY, "10");
    expect(hasStoredTermFontSize()).toBe(true);
  });

  test("no cache → default, and not an explicit choice", () => {
    expect(readStoredTermFontSize()).toBe(DEFAULT_TERM_FONT_SIZE);
    expect(hasStoredTermFontSize()).toBe(false);
  });

  test("storeTermFontSize writes the coerced value, never the raw one", () => {
    storeTermFontSize(99);
    expect(localStorage.getItem(TERM_SIZE_STORAGE_KEY)).toBe(String(TERM_FONT_SIZE_MAX));
  });
});
