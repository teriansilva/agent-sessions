import { render } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { Link, MemoryRouter, resolvePath } from "react-router-dom";

/** Mixed leading separators are a URL for another origin once a browser parses them: `/\evil.com`
 *  becomes `https://evil.com/`. react-router 7.18.0 normalizes them in resolvePath/removeDoubleSlashes
 *  (GHSA-wrjc-x8rr-h8h6, the incomplete fix for CVE-2025-68470); this pins the behaviour the SPA's
 *  navigation depends on, so a downgrade cannot quietly reintroduce the open redirect (#1237). */
const MIXED_SLASH_TARGETS = ["//evil.com", "\\\\evil.com", "/\\evil.com", "\\/evil.com"];

describe("react-router mixed-slash normalization (#1237)", () => {
  it.each(MIXED_SLASH_TARGETS)("keeps resolvePath(%j) on this origin", (to) => {
    expect(resolvePath(to).pathname).toBe("/evil.com");
  });

  it("refuses a <Link> that a browser would send to another origin", () => {
    // A browser parses /\evil.com as //evil.com — the exact hop the advisory describes. On 7.15.1
    // this renders href="/\evil.com"; the patched router refuses the external navigation instead.
    expect(() =>
      render(
        <MemoryRouter>
          <Link to="/\\evil.com">evil</Link>
        </MemoryRouter>,
      ),
    ).toThrow(/external navigation/i);
  });

  it("resolves react-router and react-router-dom above the advisory band", () => {
    // Vitest runs from `web/` (the npm scripts and CI all set that working directory).
    const lock = JSON.parse(readFileSync(resolve("package-lock.json"), "utf8")) as {
      packages: Record<string, { version?: string }>;
    };
    // The combined audit band for all five advisories ends at 7.18.1 (patched 7.18.2+).
    for (const name of ["react-router", "react-router-dom"]) {
      const version = lock.packages[`node_modules/${name}`]?.version;
      expect(version, `${name} is pinned in the lockfile`).toBeDefined();
      expect(compareVersions(version!, [7, 18, 2]), `${name}@${version}`).toBeGreaterThanOrEqual(0);
    }
  });
});

/** Numeric semver compare for the fixed 7.x band; the lockfile carries no prerelease here. */
function compareVersions(a: string, b: number[]): number {
  const parts = a.split(".").map((n) => Number.parseInt(n, 10));
  for (let i = 0; i < 3; i++) {
    const diff = (parts[i] ?? 0) - b[i];
    if (diff !== 0) return diff;
  }
  return 0;
}
