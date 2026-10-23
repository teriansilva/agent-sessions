import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { describe, expect, test } from "vitest";

/** The public viewer bundle (#1404 P4) carries none of the operator app.
 *
 *  `share.html` is what an unauthenticated visitor runs. It must not contain the operator's API
 *  client (`lib/api.ts`: the admin CSRF token, every operator route, the login bounce), the app
 *  shell, or anything that registers the service worker. This walks every local import reachable
 *  from the viewer's entry — TS/TSX and CSS `@import`/`composes` — and holds it to an allowlist,
 *  so a convenience import of an operator module fails here instead of shipping. */

const SRC = resolve(__dirname, "..");
const ENTRY = join(SRC, "share", "main.tsx");

/** The only local modules outside `src/share/` the viewer may use: pure formatting, the agent-text
 *  renderer (React elements only, no fetch), and the design tokens / base rules. */
const ALLOWED_OUTSIDE = new Set([
  "index.css",
  "tokens.css",
  // The NIGHTJAR design system (#1531), pulled in by index.css and the entry: fonts, the motion
  // rules and the primitives' CSS, plus the framework-free motion mode (localStorage + matchMedia
  // only, no network). None of them reaches the operator's API or the app shell.
  "theme/fonts.css",
  "theme/motion.css",
  "theme/motion.ts",
  "components/hud/nightjar.css",
  // The one button sheet (docs/design.md §6) the viewer's buttons compose: CSS only, no script.
  "components/ui/actionButton.module.css",
  // The brand kit's logo (#1590): an image component and the kit's static SVG files. No script
  // beyond rendering an <img>, no network of its own.
  "components/brand/Brand.tsx",
  "components/brand/Brand.module.css",
  "brand/mark-amber.svg",
  "brand/mark-dark.svg",
  "brand/wordmark-on-dark.svg",
  "brand/wordmark-on-light.svg",
  "brand/logo-horizontal-on-dark.svg",
  "brand/logo-horizontal-on-light.svg",
  "brand/logo-horizontal-notag-on-dark.svg",
  "brand/logo-horizontal-notag-on-light.svg",
  "lib/shareFormat.ts",
  "components/structured/Markdown.tsx",
  "components/structured/Markdown.module.css",
]);

const SPEC = /(?:import|export)\s[^'"]*?from\s*["']([^"']+)["']|import\s*["']([^"']+)["']|import\(\s*["']([^"']+)["']\s*\)/g;
const CSS_SPEC = /@import\s+["']([^"']+)["']|composes:[^;]*from\s+["']([^"']+)["']/g;

function resolveLocal(from: string, spec: string): string | null {
  if (!spec.startsWith(".")) return null; // a package (react, react-markdown, lucide-react…)
  const base = resolve(dirname(from), spec);
  for (const cand of [base, `${base}.ts`, `${base}.tsx`, join(base, "index.ts"), join(base, "index.tsx")]) {
    if (existsSync(cand) && statSync(cand).isFile()) return cand;
  }
  throw new Error(`unresolved import ${spec} from ${relative(SRC, from)}`);
}

/** The packages the viewer may pull in: React, the Markdown renderer and its GFM plugin, icons.
 *  Never `virtual:pwa-register` or anything else that registers a worker or reaches the app. */
const ALLOWED_PACKAGES = new Set([
  "react",
  "react-dom/client",
  "react-markdown",
  "remark-gfm",
  "lucide-react",
  // vitest-only imports in test files are never walked: the walk starts at main.tsx
]);
const packages = new Set<string>();

function walk(): Set<string> {
  const seen = new Set<string>();
  const todo = [ENTRY];
  while (todo.length) {
    const file = todo.pop()!;
    if (seen.has(file)) continue;
    seen.add(file);
    const text = readFileSync(file, "utf8");
    const re = file.endsWith(".css") ? CSS_SPEC : SPEC;
    for (const m of text.matchAll(re)) {
      const spec = m[1] ?? m[2] ?? m[3];
      if (!spec) continue;
      const next = resolveLocal(file, spec);
      if (next) todo.push(next);
      else packages.add(spec);
    }
  }
  return seen;
}

describe("the share viewer bundle", () => {
  const reached = [...walk()].map((f) => relative(SRC, f).split("\\").join("/"));

  test("reaches only its own modules and the allowlisted pure ones", () => {
    const outside = reached.filter((f) => !f.startsWith("share/") && !ALLOWED_OUTSIDE.has(f));
    expect(outside).toEqual([]);
  });

  test("pulls in only the allowlisted packages", () => {
    expect([...packages].filter((p) => !ALLOWED_PACKAGES.has(p))).toEqual([]);
  });

  test("never reaches the operator's API client or the service worker", () => {
    expect(reached).not.toContain("lib/api.ts");
    expect(reached.some((f) => /sw|swUpdate|pwa/i.test(f))).toBe(false);
    // The walk itself works: it found the viewer and the renderer it shares with the app.
    expect(reached).toContain("share/publicApi.ts");
    expect(reached).toContain("components/structured/Markdown.tsx");
  });

  test("talks only to /api/public", () => {
    const viewer = reached.filter((f) => /\.tsx?$/.test(f));
    const paths = viewer.flatMap((f) =>
      [...readFileSync(join(SRC, f), "utf8").matchAll(/["'`](\/api\/[^"'`$?]*)/g)].map((m) => m[1]),
    );
    expect(paths.length).toBeGreaterThan(0);
    expect(paths.filter((p) => p !== "/api/public" && !p.startsWith("/api/public/"))).toEqual([]);
    // No other way out: no WebSocket, beacon, operator page or socket path.
    const code = viewer
      .map((f) => readFileSync(join(SRC, f), "utf8"))
      .join("\n")
      .replace(/\/\*[\s\S]*?\*\//g, "") // comments may NAME a route; code may not call one
      .replace(/^\s*\/\/.*$/gm, "");
    expect(code).not.toMatch(/new WebSocket|sendBeacon|["'`]\/(ws|login|logout|link)\b/);
  });
});
