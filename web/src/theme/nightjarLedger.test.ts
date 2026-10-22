/** The NIGHTJAR coverage ledger (#1531, docs/nightjar-ledger.md, docs/design.md v2 §10).
 *
 *  The previous rework forgot whole areas because "done" lived in someone's head. Here it lives in
 *  a table, and this test makes the table honest: a stylesheet or a popup with no row fails, and a
 *  row marked `done` that still carries a literal colour, radius, shadow, z-index or font size
 *  fails. Reviewers add rows; they never just tick them. */
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, test } from "vitest";

import { SWEEP } from "../../visual/nightjar/manifest.mjs";

const SRC = resolve(process.cwd(), "src");
const REPO = resolve(process.cwd(), "..");
const LEDGER = join(REPO, "docs", "nightjar-ledger.md");

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(join(dir, e.name)) : [join(dir, e.name)],
  );
}
const FILES = walk(SRC).map((f) => relative(SRC, f).split("\\").join("/"));
const read = (f: string) => readFileSync(join(SRC, f), "utf8");

export interface LedgerRow {
  path: string;
  kind: "css" | "popup" | "canvas" | "page";
  phase: string;
  status: string;
  captures: string;
  motion: string;
}

export function parseLedger(md: string): LedgerRow[] {
  const rows: LedgerRow[] = [];
  for (const line of md.split("\n")) {
    const m = line.match(/^\|\s*`([^`]+)`\s*\|\s*(css|popup|canvas|page)\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|\s*$/);
    if (!m) continue;
    rows.push({
      path: m[1],
      kind: m[2] as LedgerRow["kind"],
      phase: m[3].trim(),
      status: m[4].trim(),
      captures: m[5].trim(),
      motion: m[6].trim(),
    });
  }
  return rows;
}

const rows = parseLedger(readFileSync(LEDGER, "utf8"));
const byPath = new Map(rows.map((r) => [r.path, r]));

/** Files that render a popup surface: the static floor of the inventory (docs/nightjar-ledger.md). */
const POPUP_MARKERS = /role="(dialog|menu|listbox|alertdialog)"|aria-modal|<dialog|createPortal/;
const POPUPS = FILES.filter(
  (f) => /\.tsx$/.test(f) && !/\.test\.tsx$/.test(f) && POPUP_MARKERS.test(read(f)),
);
const CSS = FILES.filter((f) => f.endsWith(".css"));

/** Code with its comments blanked, so a component that only MENTIONS `<canvas>` is not one. */
const code = (f: string) =>
  read(f)
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[\s;{}])\/\/[^\n]*/g, "$1");
/** Components that paint a canvas: a raw `<canvas>` or a React Flow graph. */
const CANVASES = FILES.filter(
  (f) => /\.tsx$/.test(f) && !/\.test\.tsx$/.test(f) && /<canvas\b|<ReactFlow\b/.test(code(f)),
);

/** Every page BattleLab serves outside the SPA's routes (docs/nightjar-ledger.md, the Page
 *  inventory): the HTML entries, the server templates, server code that returns an inline HTML
 *  document, the public site and the docs site's pages. Repo-relative. */
function repoWalk(dir: string): string[] {
  const abs = join(REPO, dir);
  if (!existsSync(abs)) return [];
  return walkAbs(abs).map((f) => relative(REPO, f).split("\\").join("/"));
}
function walkAbs(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.name === "node_modules" || e.name.startsWith(".")
      ? []
      : e.isDirectory()
        ? walkAbs(join(dir, e.name))
        : [join(dir, e.name)],
  );
}
export function discoverPages(): string[] {
  return [
    ...repoWalk("web").filter((f) => /^web\/[^/]+\.html$/.test(f)),
    ...repoWalk("src/agent_sessions/templates").filter((f) => f.endsWith(".html")),
    ...repoWalk("src/agent_sessions").filter(
      (f) => f.endsWith(".py") && /<!doctype html/i.test(readFileSync(join(REPO, f), "utf8")),
    ),
    ...repoWalk("landing").filter((f) => /^landing\/[^/]+\.html$/.test(f)),
    ...repoWalk("docs/site").filter((f) => f.endsWith(".md") && !/\/(e2e|node_modules)\//.test(f)),
  ];
}

/** A row's path up to its first space: `web/share.html → share/ShareViewer.tsx` names web/share.html. */
const head = (path: string) => path.split(" ")[0];

/** The discovered paths no row of `kind` covers. A row covers a path it names exactly, or every
 *  path under a directory it names (`docs/site pages (…)` covers every docs page). */
export function uncovered(paths: string[], rs: LedgerRow[], kind: LedgerRow["kind"]): string[] {
  const heads = rs.filter((r) => r.kind === kind).map((r) => head(r.path));
  return paths.filter(
    (p) => !heads.some((h) => h === p || (!/\.[a-z]+$/i.test(h) && p.startsWith(h.replace(/\/?$/, "/")))),
  );
}

describe("every surface has a ledger row", () => {
  test("the ledger parses", () => {
    expect(rows.length).toBeGreaterThan(100);
  });

  test("statuses are todo, done or n/a with a reason; phases are named", () => {
    const bad = rows
      .filter((r) => !(r.status === "todo" || r.status === "done" || /^n\/a: \S/.test(r.status)) || !r.phase)
      .map((r) => `${r.path}: ${r.status}`);
    expect(bad).toEqual([]);
  });

  test("no row is listed twice", () => {
    const seen = new Set<string>();
    const dup = rows.filter((r) => (seen.has(r.path) ? true : (seen.add(r.path), false))).map((r) => r.path);
    expect(dup).toEqual([]);
  });

  test("every stylesheet under web/src has a css row", () => {
    expect(CSS.filter((f) => byPath.get(f)?.kind !== "css")).toEqual([]);
  });

  test("every component rendering a dialog, menu, listbox or portal has a popup row", () => {
    expect(POPUPS.filter((f) => byPath.get(f)?.kind !== "popup")).toEqual([]);
  });

  test("every canvas (a <canvas> or a React Flow graph) has a canvas row", () => {
    expect(uncovered(CANVASES, rows, "canvas")).toEqual([]);
  });

  test("every page BattleLab serves outside the SPA has a page row", () => {
    expect(uncovered(discoverPages(), rows, "page")).toEqual([]);
  });

  test("the coverage check catches a new page or canvas with no row (negative guard)", () => {
    expect(uncovered(["web/new-thing.html"], rows, "page")).toEqual(["web/new-thing.html"]);
    expect(uncovered(["components/x/NewGraph.tsx"], rows, "canvas")).toEqual(["components/x/NewGraph.tsx"]);
    expect(uncovered(["docs/site/guide/new.md"], rows, "page")).toEqual([]); // under docs/site
    expect(discoverPages()).toContain("web/connect.html");
    expect(discoverPages()).toContain("src/agent_sessions/routes/shares.py");
    expect(CANVASES).toContain("components/overview/OverviewCanvas.tsx");
  });

  test("canvas rows, and page rows that name a repo file, name something that exists", () => {
    const gone = rows
      .filter((r) => r.kind === "canvas")
      .map((r) => head(r.path))
      .filter((p) => !existsSync(join(SRC, p)));
    const pageGone = rows
      .filter((r) => r.kind === "page" && !r.path.includes("(new)"))
      .map((r) => head(r.path))
      .filter((p) => /\//.test(p) && !p.includes("*") && !p.startsWith("battlelab-cloud"))
      .filter((p) => !existsSync(join(REPO, p)));
    expect([...gone, ...pageGone]).toEqual([]);
  });

  test("css and popup rows name files that exist", () => {
    const gone = rows
      .filter((r) => r.kind === "css" || r.kind === "popup")
      .filter((r) => !existsSync(join(SRC, r.path)))
      .map((r) => r.path);
    expect(gone).toEqual([]);
  });

  test("a done row's captures are sweep ids (web/visual/nightjar/manifest.mjs)", () => {
    const ids = new Set(SWEEP.map((c: { id: string }) => c.id));
    const unknown = rows
      .filter((r) => r.status === "done")
      .flatMap((r) => r.captures.split(/[,\s]+/).filter(Boolean).map((id) => ({ r, id })))
      .filter(({ id }) => !ids.has(id))
      .map(({ r, id }) => `${r.path}: ${id}`);
    expect(unknown).toEqual([]);
  });

  test("a done row names its captures and its motion", () => {
    const thin = rows
      .filter((r) => r.status === "done" && (!r.captures || !r.motion))
      .map((r) => r.path);
    expect(thin).toEqual([]);
  });
});

// ---------------------------------------------------------------------------------------------------
// `done` means no literals
// ---------------------------------------------------------------------------------------------------

/** Comments blanked (line breaks kept), and url(...) bodies blanked: a data-URI mask is not a colour. */
function cssBody(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, " "))
    .replace(/url\((["'])[\s\S]*?\1\)/g, (u) => u.replace(/[^\n]/g, " "));
}

/** `{ selector, decls }` for every rule body, nested or not (selector = the text before `{`). */
function rules(css: string): { selector: string; body: string; line: number }[] {
  const out: { selector: string; body: string; line: number }[] = [];
  const stack: { start: number; sel: string }[] = [];
  let last = 0;
  for (let i = 0; i < css.length; i++) {
    const ch = css[i];
    if (ch === "{") {
      stack.push({ start: i + 1, sel: css.slice(last, i).trim() });
      last = i + 1;
    } else if (ch === "}") {
      const open = stack.pop();
      if (open) {
        const body = css.slice(open.start, i);
        out.push({ selector: open.sel, body: body.replace(/\{[^{}]*\}/g, ""), line: css.slice(0, open.start).split("\n").length });
      }
      last = i + 1;
    } else if (ch === ";") last = i + 1;
  }
  return out;
}

const COLOUR = /#[0-9a-fA-F]{3,8}(?![\w-])|\brgba?\(|\bhsla?\(/;
const ROUND_OK = /led|radar|round|ping|spinner|fab|dot/i;

export function literalFindings(css: string): string[] {
  const out: string[] = [];
  for (const r of rules(cssBody(css))) {
    if (r.selector.startsWith("@keyframes") || /^(from|to|\d+%)/.test(r.selector)) continue;
    for (const raw of r.body.split(";")) {
      const d = raw.trim();
      const i = d.indexOf(":");
      if (i < 0) continue;
      const prop = d.slice(0, i).trim().toLowerCase();
      const val = d.slice(i + 1).trim();
      if (prop.startsWith("--")) continue; // a local custom property is a token, read below
      const at = `${r.selector.split("\n").pop()} { ${prop} } (line ~${r.line})`;
      if (COLOUR.test(val)) out.push(`${at}: literal colour ${val}`);
      if (prop === "border-radius" && !/^0(px)?$/.test(val) && !ROUND_OK.test(r.selector))
        out.push(`${at}: radius ${val}`);
      if (prop === "z-index" && /^-?\d+$/.test(val) && val !== "0") out.push(`${at}: z-index ${val}`);
      if (prop === "font-size" && !/^var\(--fs-[\w-]+\)$|^inherit$/.test(val)) out.push(`${at}: font-size ${val}`);
      if (prop === "font" && /\b\d+(\.\d+)?(px|rem|em|pt)\b/.test(val.split("/")[0]))
        out.push(`${at}: font size literal in ${val}`);
      if (prop === "box-shadow" && val !== "none" && !/var\(--(shadow|glow)-/.test(val)) {
        // An inset hairline with no blur is a border drawn inside a clip-path; anything that blurs
        // is depth or glow and must be a token.
        const blurred = val.split(/,(?![^(]*\))/).some((s) => {
          const lengths = s
            .replace(/var\([^)]*\)/g, " ")
            .trim()
            .split(/\s+/)
            .filter((t) => /^-?\d+(\.\d+)?(px|rem|em)?$/.test(t));
          return lengths.length >= 3 && parseFloat(lengths[2]) > 0;
        });
        if (blurred) out.push(`${at}: shadow geometry ${val}`);
      }
    }
  }
  return out;
}

describe("a done stylesheet carries no literals (docs/design.md v2 §10)", () => {
  test("the checker catches what it is for", () => {
    expect(literalFindings(".a { color: #fff; }")).toHaveLength(1);
    expect(literalFindings(".a { background: rgba(0,0,0,.5); }")).toHaveLength(1);
    expect(literalFindings(".a { border-radius: 4px; }")).toHaveLength(1);
    expect(literalFindings(".led { border-radius: 50%; }")).toEqual([]);
    expect(literalFindings(".a { z-index: 5; }")).toHaveLength(1);
    expect(literalFindings(".a { z-index: var(--z-menu); }")).toEqual([]);
    expect(literalFindings(".a { font-size: 12px; }")).toHaveLength(1);
    expect(literalFindings(".a { font: 500 12px/1 var(--font-mono); }")).toHaveLength(1);
    expect(literalFindings(".a { font: 500 var(--fs-label) / 1 var(--font-mono); }")).toEqual([]);
    expect(literalFindings(".a { box-shadow: 0 4px 12px black; }")).toHaveLength(1);
    expect(literalFindings(".a { box-shadow: inset 0 -2px 0 var(--hud); }")).toEqual([]);
    expect(literalFindings(".a { box-shadow: var(--shadow-menu); }")).toEqual([]);
    expect(literalFindings("/* #fff */ .a { color: var(--text-1); }")).toEqual([]);
    expect(literalFindings(".a { mask: url(\"data:image/svg+xml;utf8,<svg stroke='#fff'/>\"); }")).toEqual([]);
  });

  for (const r of rows.filter((x) => x.kind === "css" && x.status === "done")) {
    test(r.path, () => {
      expect(literalFindings(read(r.path))).toEqual([]);
    });
  }
});
