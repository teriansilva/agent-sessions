import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { describe, expect, test } from "vitest";
import { FRAME_SANDBOX } from "./ArtifactViewer";

/** The public artifact viewer (#2068) is its own bundle, like the session-share and reply viewers:
 *  this walks every local import from `artifact/main.tsx` and holds it to an allowlist — no
 *  operator API client, no service worker, no `/api` route at all: its one read is the link's
 *  `~meta`. (Modelled on `reply/replyBundle.test.ts`.) */

const SRC = resolve(__dirname, "..");
const ENTRY = join(SRC, "artifact", "main.tsx");

/** The only local modules outside `src/share/` the viewer may use: pure formatting, the agent-text
 *  renderer (React elements only, no fetch), and the design tokens / base rules. */
const ALLOWED_OUTSIDE = new Set([
  // The session-share viewer's stylesheet: the artifact page wears the same notices and chips.
  "share/ShareViewer.module.css",
  "index.css",
  "tokens.css",
  // The NIGHTJAR design system (#1531), pulled in by index.css and the entry: fonts, the motion
  // rules and the primitives' CSS, plus the framework-free motion mode (localStorage + matchMedia
  // only, no network). None of them reaches the operator's API or the app shell.
  "theme/fonts.css",
  "theme/motion.css",
  "theme/motion.ts",
  "components/hud/nightjar.css",
  // The page theme (#1596: the device's stored choice, else its colour scheme — localStorage and
  // matchMedia only) and two markup-only primitives: the atmosphere and the loader.
  "theme/applyTheme.ts",
  "theme/themes.ts",
  "components/hud/Atmosphere.tsx",
  "components/hud/Loader.tsx",
  // The one button sheet (docs/design.md §6) the viewer's buttons compose: CSS only, no script.
  "components/ui/actionButton.module.css",
  // The brand kit's logo (#1590): an image component and the kit's static SVG files. No script
  // beyond rendering an <img>, no network of its own.
  // The header of every public page (#2068): markup, a sessionStorage flag, and the one
  // battlelabos.com constant (links.ts holds URL constants and pure builders only, no fetch).
  "components/public/PublicBar.tsx",
  "components/public/PublicBar.module.css",
  "lib/links.ts",
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

describe("the public artifact viewer bundle (#2068)", () => {
  const reached = [...walk()].map((f) => relative(SRC, f).split("\\").join("/"));

  test("reaches only its own modules and the allowlisted pure ones", () => {
    const outside = reached.filter((f) => !f.startsWith("artifact/") && !ALLOWED_OUTSIDE.has(f));
    expect(outside).toEqual([]);
  });

  test("pulls in only the allowlisted packages", () => {
    expect([...packages].filter((p) => !ALLOWED_PACKAGES.has(p))).toEqual([]);
  });

  test("never reaches the operator's API client or the service worker", () => {
    expect(reached).not.toContain("lib/api.ts");
    expect(reached.some((f) => /sw|swUpdate|pwa/i.test(f))).toBe(false);
    // The walk itself works: it found the viewer, its one read and the shared header.
    expect(reached).toContain("artifact/publicArtifact.ts");
    expect(reached).toContain("components/public/PublicBar.tsx");
  });

  test("talks to no /api route: its one read is the link's ~meta", () => {
    const viewer = reached.filter((f) => /\.tsx?$/.test(f));
    const code = viewer
      .map((f) => readFileSync(join(SRC, f), "utf8"))
      .join("\n")
      .replace(/\/\*[\s\S]*?\*\//g, "") // comments may NAME a route; code may not call one
      .replace(/^\s*\/\/.*$/gm, "");
    expect(code).not.toMatch(/["'`]\/api\//);
    // Exactly one fetch, of the metadata, cookie-less.
    expect(code.match(/\bfetch\(/g) ?? []).toHaveLength(1);
    expect(code).toMatch(/fetch\(`\/artifacts\/p\/\$\{id\}\/~meta`/);
    expect(code).toMatch(/credentials: "omit"/);
    // No other way out: no WebSocket, beacon, operator page or socket path.
    expect(code).not.toMatch(/new WebSocket|sendBeacon|["'`]\/(ws|login|logout|link)\b/);
  });

  test("the frame never gets allow-same-origin", () => {
    const viewer = readFileSync(join(SRC, "artifact", "ArtifactViewer.tsx"), "utf8");
    expect(viewer).toMatch(/sandbox=\{FRAME_SANDBOX\}/);
    expect(FRAME_SANDBOX.split(/\s+/)).not.toContain("allow-same-origin");
    expect(FRAME_SANDBOX.split(/\s+/)).toContain("allow-scripts");
  });
});
