import { beforeEach, expect, test } from "vitest";
import { isFontAvailable, resetFontAvailability } from "./fontAvailable";
import type { TextMeasurer } from "./fontAvailable";

// A deterministic stand-in for canvas text measurement. jsdom has no canvas, so the real probe
// cannot run here — and mocking it away would test nothing. Instead this models the ONE
// behaviour that matters: the browser resolves the first family in the list that exists, and
// falls through to the base otherwise. Widths are per-face constants, so "did the width move?"
// means exactly "did a face before the base resolve?".
const WIDTH: Record<string, number> = {
  monospace: 500, // on this fake device, monospace IS "Installed Mono" (see below)
  "sans-serif": 640,
  serif: 610,
  "Installed Mono": 500, // deliberately IDENTICAL to the monospace default
  "Other Mono": 555,
};

function measurer(installed: string[]): TextMeasurer {
  return {
    font: "",
    measureText() {
      // `${size}px ${family}` — take the family list, resolve it the way a browser would.
      const list = this.font.slice(this.font.indexOf("px ") + 3);
      for (const raw of list.split(",")) {
        const name = raw.trim().replace(/^["']|["']$/g, "");
        if (installed.includes(name) || name in WIDTH === false) {
          if (installed.includes(name)) return { width: WIDTH[name] };
          continue; // a name nothing knows about: fall through, like a real font stack
        }
        if (name === "monospace" || name === "sans-serif" || name === "serif")
          return { width: WIDTH[name] };
      }
      return { width: WIDTH["monospace"] };
    },
  };
}

beforeEach(() => resetFontAvailability());

test("a face that IS present resolves true", () => {
  expect(isFontAvailable("Other Mono", measurer(["Other Mono"]))).toBe(true);
});

test("a MISSING primary with a PRESENT fallback resolves false", () => {
  // The case the whole module exists for. Asking about the STACK
  // (`"Cascadia Mono", …, monospace`) always answers yes, because monospace resolves
  // everywhere — so a stack-level check reports every preset as installed and the greyed-out
  // card state becomes a lie. Here the fallback is present and the primary is not.
  expect(isFontAvailable("Cascadia Mono", measurer(["Other Mono"]))).toBe(false);
});

test("an installed face whose metrics MATCH the monospace default still resolves true", () => {
  // Measured on the real host (#866): on a stock Linux box `monospace` IS DejaVu Sans Mono, so
  // "DejaVu Sans Mono, monospace" vs "monospace" differs by exactly 0 — a single-base probe
  // would grey out a card that works. The sans-serif/serif bases are what rescue it.
  expect(isFontAvailable("Installed Mono", measurer(["Installed Mono"]))).toBe(
    true,
  );
});

test("nothing installed at all resolves false, for every name", () => {
  const m = measurer([]);
  expect(isFontAvailable("Installed Mono", m)).toBe(false);
  expect(isFontAvailable("Other Mono", m)).toBe(false);
  expect(isFontAvailable("Definitely Not A Font", m)).toBe(false);
});

test("an empty or whitespace name is not a font", () => {
  expect(isFontAvailable("", measurer(["Other Mono"]))).toBe(false);
  expect(isFontAvailable("   ", measurer(["Other Mono"]))).toBe(false);
});

test("results are cached per family, but only for the REAL measurer", () => {
  // The cache exists because Settings re-renders on every keystroke in the custom field. An
  // injected measurer bypasses it so tests can't leak state into each other.
  let calls = 0;
  const counting: TextMeasurer = {
    font: "",
    measureText() {
      calls += 1;
      return { width: 500 };
    },
  };
  isFontAvailable("Other Mono", counting);
  const first = calls;
  isFontAvailable("Other Mono", counting);
  expect(calls).toBe(first * 2);
});

test("when it cannot measure at all, it does NOT claim the face is missing", () => {
  // jsdom has no canvas, so the default measurer is null here — which is exactly the
  // production case of a hardened embedding. Greying out a card that would work hides a
  // working choice; every preset stack has a fallback, so the opposite error is harmless.
  expect(isFontAvailable("Other Mono")).toBe(true);
});
