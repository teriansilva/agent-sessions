/** One touch-target floor for the e2e suite (e2e/touchTarget.ts). A box measured while a NIGHTJAR
 *  arrival or deploy transform runs reads 43.99997 px, so a bare `toBeGreaterThanOrEqual(44)` is a
 *  flake waiting for a slow runner (three CI failures on #1584 in one day). Every 44 px assertion
 *  reads `TOUCH_FLOOR`, which tolerates that float residue and no real violation. */
import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { expect, test } from "vitest";

import { PX_EPSILON, TOUCH_FLOOR, TOUCH_MIN } from "../../e2e/touchTarget";

const E2E = resolve(process.cwd(), "e2e");

function specs(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((d) =>
    d.isDirectory() ? specs(join(dir, d.name)) : /\.ts$/.test(d.name) ? [join(dir, d.name)] : [],
  );
}

test("no e2e assertion compares a measured box with a bare 44", () => {
  // The literal floor, however it is spaced: `toBeGreaterThanOrEqual( 44 )`. (Three older checks
  // already tolerate with 43.5 — not a flake, so they are left as they are.)
  const bare = /toBeGreaterThanOrEqual\(\s*44\s*\)/;
  const offenders = specs(E2E)
    .filter((f) => !f.endsWith("touchTarget.ts"))
    .flatMap((f) =>
      readFileSync(f, "utf8")
        .split("\n")
        .map((line, i) => [line, i] as const)
        .filter(([line]) => bare.test(line))
        .map(([, i]) => `${f.slice(E2E.length + 1)}:${i + 1}`),
    );
  expect(offenders).toEqual([]);
});

test("the floor tolerates float residue and nothing a pixel could hide", () => {
  expect(TOUCH_MIN).toBe(44);
  expect(43.99997).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  // One device pixel short on the densest phones (3×) is a real violation and must still fail.
  expect(44 - 1 / 3).toBeLessThan(TOUCH_FLOOR);
  expect(PX_EPSILON).toBeLessThan(0.05);
});
