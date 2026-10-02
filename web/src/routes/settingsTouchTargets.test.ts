import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "vitest";

// `docs/design.md` §8: touch targets ≥44px. jsdom computes no layout, so geometry cannot be
// measured from a mounted component — the declaration is the thing that can be checked, and it
// is the thing that was wrong: the per-agent budget controls shipped at 34px, and the notify
// checkbox at 20px under a comment *claiming* the label supplied the hit area while nothing
// made the label 44px either (#839).
const css = readFileSync(
  resolve(process.cwd(), "src/routes/Settings.module.css"),
  "utf8",
).replace(/\/\*[\s\S]*?\*\//g, "");

function declarations(selector: string): string {
  // EVERY rule naming this selector, concatenated — CSS cascades, and these controls are styled
  // by a shared `.budgetField, .budgetToggle` block plus their own. Reading only the first match
  // would report "no min-height" for a selector that has one two rules later.
  const re = new RegExp(
    `(?:^|\\n)[^{}]*\\${selector}(?![\\w-])[^{}]*\\{([^}]*)\\}`,
    "g",
  );
  const bodies = [...css.matchAll(re)].map((m) => m[1]);
  expect(bodies.length, `rule ${selector} present`).toBeGreaterThan(0);
  return bodies.join(";");
}

function minHeightPx(selector: string): number {
  const all = [
    ...declarations(selector).matchAll(/min-height:\s*(\d+(?:\.\d+)?)px/g),
  ];
  expect(all.length, `${selector} declares a min-height`).toBeGreaterThan(0);
  return Math.max(...all.map((m) => Number(m[1])));
}

test.each([
  [".budgetPct", "the alert-threshold field"],
  [".budgetTokens", "the per-agent limit and used fields"],
  [".budgetToggle", "the notify label — the checkbox's real hit area"],
  [".budgetRefresh", "the ask-the-agents button"],
])("%s meets the 44px touch target (%s)", (selector) => {
  expect(minHeightPx(selector)).toBeGreaterThanOrEqual(44);
});

test("the notify checkbox's hit area is the label, not the 20px box", () => {
  // The input stays small on purpose — a 44px checkbox looks broken on a desktop pointer. What
  // matters is that the thing wrapping it is 44px, which is what a finger actually lands on.
  expect(minHeightPx(".budgetToggle")).toBeGreaterThanOrEqual(44);
  const input = declarations(".budgetToggle input");
  expect(input).toMatch(/min-height:\s*\d+px/);
});
