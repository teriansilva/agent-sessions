import { expect, test } from "vitest";
import {
  ACCENT_PRESETS,
  coerceAccent,
  DEFAULT_ACCENT,
  isAccent,
  normalizeAccent,
  onAccentContrast,
  onAccentFor,
} from "./accent";

test("normalizeAccent: accepts #rrggbb, bare hex, uppercase → lowercase #rrggbb", () => {
  expect(normalizeAccent("#FFB000")).toBe("#ffb000");
  expect(normalizeAccent("ffb000")).toBe("#ffb000");
  expect(normalizeAccent("  #C02020  ")).toBe("#c02020");
});

test("normalizeAccent: expands #rgb shorthand", () => {
  expect(normalizeAccent("#0af")).toBe("#00aaff");
  expect(normalizeAccent("abc")).toBe("#aabbcc");
});

test("normalizeAccent: rejects non-hex / wrong length / non-strings", () => {
  for (const bad of [
    "nope",
    "#12",
    "#12345",
    "#1234567",
    "rgb(0,0,0)",
    "",
    "#ggghhh",
    7,
    null,
    undefined,
  ]) {
    expect(normalizeAccent(bad as unknown)).toBeNull();
  }
});

test("coerceAccent falls back to the default for garbage; isAccent narrows", () => {
  expect(coerceAccent("teal")).toBe(DEFAULT_ACCENT);
  expect(coerceAccent("#3fbf6f")).toBe("#3fbf6f");
  expect(isAccent("#3fbf6f")).toBe(true);
  expect(isAccent("teal")).toBe(false);
});

test("default accent is phosphor-amber (mirror of prefs.py)", () => {
  expect(DEFAULT_ACCENT).toBe("#ffb000");
});

test("onAccentFor picks dark ink on a light accent, white on a dark accent", () => {
  expect(onAccentFor("#ffb000")).toBe("#0b0b0d"); // light amber → dark ink
  expect(onAccentFor("#c02020")).toBe("#ffffff"); // dark red → white ink
});

test("every preset is a normalized hex AND its on-accent ink meets WCAG AA (>=4.5:1)", () => {
  const ids = new Set<string>();
  for (const p of ACCENT_PRESETS) {
    expect(normalizeAccent(p.hex), `${p.id} is normalized`).toBe(p.hex);
    expect(
      onAccentContrast(p.hex),
      `${p.id} on-accent AA`,
    ).toBeGreaterThanOrEqual(4.5);
    expect(ids.has(p.id), `${p.id} unique`).toBe(false);
    ids.add(p.id);
  }
  // The default must be offered as a preset so a customized user can get back to it.
  expect(ACCENT_PRESETS.some((p) => p.hex === DEFAULT_ACCENT)).toBe(true);
});
