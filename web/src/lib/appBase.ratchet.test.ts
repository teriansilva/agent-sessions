import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { expect, test } from "vitest";

/** #1735: under the relay the app lives at `/connect/`, and the landing host sends every other
 *  path away. A navigation that bypasses the router with a literal root path (`/login`, `/templates`)
 *  leaves the app there, so they go through `appUrl()` (lib/appBase.ts) instead. */

const SRC = resolve(process.cwd(), "src");
const ROOT_NAV = [
  /location\.(?:assign|replace)\(\s*["'`]\//,
  /location\.href\s*=\s*["'`]\//,
  /window\.open\(\s*["'`]\//,
  /\shref=["']\/[A-Za-z]/,
];

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) return sources(full);
    return /\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name) ? [full] : [];
  });
}

test("no app code navigates to a literal root path outside the router", () => {
  const offenders = sources(SRC).flatMap((f) =>
    readFileSync(f, "utf8")
      .split("\n")
      .map((line, i) => ({ line, at: `${relative(SRC, f)}:${i + 1}` }))
      .filter(({ line }) => ROOT_NAV.some((re) => re.test(line)))
      .map(({ at, line }) => `${at}  ${line.trim()}`),
  );
  expect(offenders).toEqual([]);
});

test("the ratchet is not vacuous: each pattern catches its shape", () => {
  for (const sample of [
    'location.assign("/login")',
    "location.href = '/x'",
    'window.open("/p/1")',
    ' href="/templates"',
  ]) {
    expect(ROOT_NAV.some((re) => re.test(sample))).toBe(true);
  }
  expect(ROOT_NAV.some((re) => re.test('location.assign(appUrl("/login"))'))).toBe(false);
});
