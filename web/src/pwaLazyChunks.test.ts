import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";
import { isLazyOnlyChunk, lazyOnlyPackages, packageKeyOf, type Lockfile } from "./pwaLazyChunks";

/** #2114: which chunks stay out of the precache. The build itself also refuses a Mermaid chunk
 *  outside `assets/lazy/` and an sw.js that names one (vite.config.ts,
 *  `lazyChunksStayOutOfPrecache`); these pin the package walk it relies on. */

const LOCK: Lockfile = {
  packages: {
    "": { dependencies: { mermaid: "1", "@xyflow/react": "1", react: "1" } },
    "node_modules/mermaid": { dependencies: { khroma: "1", "d3-selection": "1", dagre: "1" } },
    "node_modules/khroma": {},
    "node_modules/dagre": { dependencies: { lodash: "1" } },
    // dagre's own nested copy wins over the hoisted one.
    "node_modules/dagre/node_modules/lodash": {},
    "node_modules/lodash": {},
    "node_modules/@xyflow/react": { dependencies: { "d3-selection": "1" } },
    "node_modules/d3-selection": {},
    "node_modules/react": {},
  },
};

describe("lazyOnlyPackages", () => {
  test("what only the lazy root reaches — never a package the app also needs", () => {
    expect([...lazyOnlyPackages(LOCK, ["mermaid"])].sort()).toEqual([
      "node_modules/dagre",
      "node_modules/dagre/node_modules/lodash",
      "node_modules/khroma",
      "node_modules/mermaid",
    ]);
  });

  test("against the real lockfile: mermaid is lazy, the app's own libraries are not", () => {
    const lock = JSON.parse(readFileSync(resolve(process.cwd(), "package-lock.json"), "utf8")) as Lockfile;
    const lazy = lazyOnlyPackages(lock);
    expect(lazy.has("node_modules/mermaid")).toBe(true);
    for (const shared of ["node_modules/react", "node_modules/@xyflow/react", "node_modules/d3-selection"]) {
      expect(lazy.has(shared), shared).toBe(false);
    }
  });
});

test("packageKeyOf", () => {
  expect(packageKeyOf("/w/web/node_modules/khroma/dist/index.js")).toBe("node_modules/khroma");
  expect(packageKeyOf("/w/web/node_modules/@xyflow/react/dist/esm/index.js")).toBe("node_modules/@xyflow/react");
  expect(packageKeyOf("/w/web/node_modules/dagre/node_modules/lodash/x.js?commonjs-es-import")).toBe(
    "node_modules/dagre/node_modules/lodash",
  );
  expect(packageKeyOf("/w/web/src/App.tsx")).toBeNull();
});

test("isLazyOnlyChunk: every module must be lazy-only; app code or a shared package keeps it precached", () => {
  const lazy = lazyOnlyPackages(LOCK, ["mermaid"]);
  expect(isLazyOnlyChunk(["/w/node_modules/mermaid/a.js", "/w/node_modules/khroma/b.js"], lazy)).toBe(true);
  expect(isLazyOnlyChunk(["/w/node_modules/mermaid/a.js", "/w/node_modules/d3-selection/b.js"], lazy)).toBe(false);
  expect(isLazyOnlyChunk(["/w/node_modules/mermaid/a.js", "/w/src/x.ts"], lazy)).toBe(false);
  expect(isLazyOnlyChunk([], lazy)).toBe(false);
});
