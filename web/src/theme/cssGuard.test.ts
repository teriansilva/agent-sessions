/** No undefined tokens, and no hard-coded purple, in the app's styles (#948).
 *
 * The report was a purple dialog. The cause was a stylesheet written against tokens that tokens.css
 * never defined (`--surface-1`, `--mono`, `--focus`). `var(--surface-1, #1b1226)` looks themed in a
 * diff and paints its fallback on every install, in both themes, because the token it names does not
 * exist. Copies of that sheet reached three dialogs and a Settings form before anyone saw it, because
 * nothing broke. These checks are the part of that review a machine can do.
 *
 * What counts as defined: a name tokens.css declares, a custom property some other sheet declares
 * (`--rec` in Compose, `--app-header-height` in App.css), or one the app sets from code
 * (`style={{ "--proj": colour }}`, `setProperty("--sidebar-w", …)`). It is a name-level check, not a
 * cascade check: a local property declared in one sheet and read in another would pass. The failure
 * it exists for is a name that nothing sets at all.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, test } from "vitest";

const SRC = resolve(process.cwd(), "src");

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(join(dir, e.name)) : [join(dir, e.name)],
  );
}

const FILES = walk(SRC).map((f) => relative(SRC, f).split("\\").join("/"));
const read = (f: string) => readFileSync(join(SRC, f), "utf8");

/** Comments blanked with their line breaks kept, so a finding still names its line. */
const blank = (c: string) => c.replace(/[^\n]/g, " ");
const cssSource = (f: string) => read(f).replace(/\/\*[\s\S]*?\*\//g, blank);
// A `//` comment only where it cannot be the middle of a URL (`https://…` has a colon before it).
const codeSource = (f: string) =>
  read(f)
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/(^|[\s;{}])\/\/[^\n]*/g, (m, lead: string) => lead + blank(m.slice(lead.length)));

const ALL_CSS = FILES.filter((f) => f.endsWith(".css"));
const CSS = ALL_CSS.filter((f) => f !== "tokens.css");
const CODE = FILES.filter((f) => /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f));

function findings(files: string[], source: (f: string) => string, re: RegExp) {
  return files.flatMap((f) =>
    source(f)
      .split("\n")
      .flatMap((line, i) => [...line.matchAll(re)].map((m) => ({ at: `${f}:${i + 1}`, m }))),
  );
}

// ---------------------------------------------------------------------------------------------------
// Undefined tokens
// ---------------------------------------------------------------------------------------------------

/** Custom properties code SETS: an object key (`{ "--proj": c }`, `{ ["--proj" as string]: c }`) or a
 *  `setProperty("--name", …)` call. A quoted name anywhere else, such as `getPropertyValue("--x")`, is
 *  a read, and counting it as a definition would let an undefined token pass (#961 review 4813). */
function codeDefinitions(src: string): string[] {
  const out: string[] = [];
  for (const m of src.matchAll(/["'`](--[\w-]+)["'`]\s*(?:as\s+[^\]:,}]+)?\]?\s*:/g)) out.push(m[1]);
  for (const m of src.matchAll(/setProperty\(\s*["'`](--[\w-]+)["'`]/g)) out.push(m[1]);
  return out;
}

describe("every var(--name) the app reads is defined somewhere (#948)", () => {
  const defined = new Set<string>();
  for (const f of ALL_CSS) for (const m of cssSource(f).matchAll(/(--[\w-]+)\s*:/g)) defined.add(m[1]);
  for (const f of CODE) for (const name of codeDefinitions(codeSource(f))) defined.add(name);

  test("a READ in code is not a definition (#961 review 4813)", () => {
    expect(codeDefinitions('el.style.getPropertyValue("--missing")')).toEqual([]);
    expect(codeDefinitions('getComputedStyle(el).getPropertyValue("--missing")')).toEqual([]);
    expect(codeDefinitions('style={{ "--proj": colour }}')).toEqual(["--proj"]);
    expect(codeDefinitions('style={{ ["--proj" as string]: colour }}')).toEqual(["--proj"]);
    expect(codeDefinitions('document.documentElement.style.setProperty("--sidebar-w", w)')).toEqual(["--sidebar-w"]);
  });

  test("in stylesheets", () => {
    const missing = findings(ALL_CSS, cssSource, /var\(\s*(--[\w-]+)/g)
      .filter(({ m }) => !defined.has(m[1]))
      .map(({ at, m }) => `${at} ${m[1]}`);
    expect(missing).toEqual([]);
  });

  test("in inline styles set from code", () => {
    const missing = findings(CODE, codeSource, /var\(\s*(--[\w-]+)/g)
      .filter(({ m }) => !defined.has(m[1]))
      .map(({ at, m }) => `${at} ${m[1]}`);
    expect(missing).toEqual([]);
  });
  test("every pre-existing hook is still a missing read (remove an entry once fixed)", () => {
    const stale = [...PREEXISTING_HOOKS].filter((k) => {
      const [f, ref] = k.split(":");
      const [local, name] = ref.split(".");
      const src = codeSource(f).replace(/^import[^\n]*$/gm, "");
      const imp = codeSource(f).match(new RegExp(`import\\s+${local}\\s+from\\s+["'](\\.[^"']+\\.module\\.css)["']`));
      if (!imp || !new RegExp(`\\b${local}\\.${name}\\b`).test(src)) return true;
      const cssFile = relative(SRC, resolve(SRC, f, "..", imp[1])).split("\\").join("/");
      return moduleClasses(cssFile).has(name);
    });
    expect(stale).toEqual([]);
  });
});

// ---------------------------------------------------------------------------------------------------
// Hard-coded purple
// ---------------------------------------------------------------------------------------------------

/** HSL hue (degrees) and saturation (0–1) of `#rgb`, `#rgba`, `#rrggbb` or `#rrggbbaa`. Alpha is ignored. */
function hueSat(hex: string): { hue: number; sat: number } {
  let h = hex.replace(/^#/, "");
  if (h.length <= 4) h = [...h.slice(0, 3)].map((c) => c + c).join("");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  if (d === 0) return { hue: 0, sat: 0 };
  const l = (max + min) / 2;
  const sat = d / (1 - Math.abs(2 * l - 1));
  let hue = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  hue *= 60;
  if (hue < 0) hue += 360;
  return { hue, sat };
}

/** Indigo through violet. It starts at 240° because the retired panel blue-violet (`#14102e`) sits
 *  at 248°. It stops at 320° so the magenta and pink the app does use stay out of it: the Kimi engine
 *  colour is at 329° and the operator's Magenta accent preset at 322°. Under 15% saturation a hex
 *  is a tinted grey, and the HUD palette is made of those. */
const isPurple = (hex: string) => {
  const { hue, sat } = hueSat(hex);
  return hue >= 240 && hue <= 320 && sat >= 0.15;
};

/** The only purples allowed, each for a reason that is not chrome. Keyed by file AND value, so a new
 *  purple in the same file still fails. */
const PURPLE_ALLOWED: Record<string, readonly string[]> = {
  // The terminal's ANSI palette. Magenta is one of the sixteen colours a program asks for by number,
  // and swapping in another hue would misrender agent output. These paint only the xterm canvas.
  "theme/themes.ts": ["#75507b", "#ad7fa8", "#8f2f96", "#7a3fd4"],
  // The Violet accent preset (#1494). An accent is the one colour the operator picks for the chrome,
  // so it is theirs to make purple; it applies only when chosen, and the default stays amber. The
  // project colours are the same list (#1508; the old six-colour palette held #c792ea), so a violet
  // project marker is likewise the operator's choice.
  "theme/accent.ts": ["#8b5cf6"],
  // Third-party terminal colour schemes (#1494 P2), at their published values — same reason as the
  // ANSI palette above: magenta is a colour a program asks for by number. Catppuccin Mocha's ground
  // (#1e1e2e) is violet-tinted by design. All of them paint only a terminal the operator chose
  // that scheme for, never the chrome.
  "theme/termSchemes.ts": [
    "#1e1e2e", "#8250df", "#a475f9", "#a626a4", "#b48ead", "#bb9af7", "#bc8cff",
    "#bd93f9", "#c678dd", "#d2a8ff", "#d6acff", "#ea76cb", "#f5c2e7", "#ff92df",
  ],
};

const allowed = (at: string, hex: string) =>
  (PURPLE_ALLOWED[at.slice(0, at.lastIndexOf(":"))] ?? []).includes(hex.toLowerCase());

describe("no hard-coded purple outside tokens.css (#948)", () => {
  test("the classifier is right about the colours this was written for", () => {
    // Without these, a broken hue function would pass the scans below by finding nothing.
    for (const hex of ["#1b1226", "#2a223f", "#3b3357", "#aea3c7", "#ece7f7", "#0b0617", "#2f2658", "#14102e", "#a78bfa"])
      expect(isPurple(hex), hex).toBe(true);
    // The amber accent, the draft blue, the Gemini blue, the Kimi pink, the Magenta accent preset,
    // the HUD greys.
    for (const hex of ["#ffb000", "#3b9eff", "#7aa2ff", "#f472b6", "#d6409f", "#25282e", "#6f747d", "#131418"])
      expect(isPurple(hex), hex).toBe(false);
  });

  test("in stylesheets", () => {
    const hits = findings(CSS, cssSource, /#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\w-])/g)
      .filter(({ at, m }) => isPurple(m[0]) && !allowed(at, m[0]))
      .map(({ at, m }) => `${at} ${m[0]}`);
    expect(hits).toEqual([]);
  });

  test("in code", () => {
    // Six- and eight-digit only, plus a fully quoted short form. `#335` in a string is an issue
    // number far more often than a colour.
    const hits = findings(CODE, codeSource, /#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6})(?![\w-])|(?<=["'`])#[0-9a-fA-F]{3,4}(?=["'`])/g)
      .filter(({ at, m }) => isPurple(m[0]) && !allowed(at, m[0]))
      .map(({ at, m }) => `${at} ${m[0]}`);
    expect(hits).toEqual([]);
  });
});

// ---------------------------------------------------------------------------------------------------
// The accent is a fill, not a text colour (#1494)
// ---------------------------------------------------------------------------------------------------

/** `--accent` is sized as a FILL: with the pale presets (Ice, Yellow, Sky) it is ~1.1–1.9:1 as text
 *  on the light ground. Text — the `color` property — reads `--accent-text`, which accentTextFor()
 *  keeps at AA for every preset in both modes. Fills, borders, outlines and shadows keep the raw
 *  accent. */
describe("no text colour reads the raw --accent (#1494)", () => {
  // The raw accent ANYWHERE in a `color` value: directly, as a var() fallback
  // (`var(--brand, var(--accent))`), or inside a color-mix — each paints raw-accent text somewhere.
  const RAW_TEXT = /(?<![-\w])color:[^;{}\n]*var\(\s*--accent\s*[,)]/g;
  test("in stylesheets", () => {
    expect(findings(CSS, cssSource, RAW_TEXT).map(({ at }) => at)).toEqual([]);
  });
  test("in inline styles set from code", () => {
    expect(findings(CODE, codeSource, RAW_TEXT).map(({ at }) => at)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------------------------------
// Motion budget (#1531, docs/design.md v2 §8)
// ---------------------------------------------------------------------------------------------------

/** What a keyframe may animate: compositor-friendly properties only. `clip-path` and box geometry
 *  are never animated (#211: a clip-path mid-change drops clicks), and nothing else forces layout
 *  or paint on every frame. */
const KEYFRAME_PROPS = new Set([
  "transform",
  "opacity",
  "filter",
  "stroke-dashoffset",
  "offset",
  "animation-timing-function",
]);
/** Keyframes that predate the budget, each pinned to the phase that redraws it. Keyed by file and
 *  keyframe name, so a new violation in the same file still fails. Remove an entry with its fix. */
const KEYFRAME_PENDING: Record<string, readonly string[]> = {
  // Phase 9: the map's pulse and window flash become opacity on a pseudo-element.
  "components/overview/overview.css": ["tr-ov-pulse"],
  "components/overview/windowLayer.module.css": ["window-flash"],
  // Phase 10: the suggestions caret blinks with opacity like `.nj-caret`.
  "components/templates/SuggestionsPanel.module.css": ["blink"],
};

function keyframes(css: string): { name: string; props: string[] }[] {
  const out: { name: string; props: string[] }[] = [];
  for (const m of css.matchAll(/@keyframes\s+([\w-]+)\s*\{/g)) {
    let depth = 0;
    let j = (m.index ?? 0) + m[0].length - 1;
    const start = j;
    for (; j < css.length; j++) {
      if (css[j] === "{") depth++;
      else if (css[j] === "}" && --depth === 0) break;
    }
    const body = css.slice(start, j);
    out.push({ name: m[1], props: [...body.matchAll(/([\w-]+)\s*:/g)].map((p) => p[1].toLowerCase()) });
  }
  return out;
}

describe("keyframes animate only transform, opacity, filter and stroke-dashoffset (#1531)", () => {
  test("the parser sees what it is for", () => {
    expect(keyframes("@keyframes a { from { clip-path: inset(0) } to { opacity: 1 } }")).toEqual([
      { name: "a", props: ["clip-path", "opacity"] },
    ]);
  });
  test("in every stylesheet", () => {
    const bad = ALL_CSS.flatMap((f) =>
      keyframes(cssSource(f))
        .filter((k) => !(KEYFRAME_PENDING[f] ?? []).includes(k.name))
        .flatMap((k) =>
          k.props.filter((p) => !KEYFRAME_PROPS.has(p)).map((p) => `${f} @keyframes ${k.name}: ${p}`),
        ),
    );
    expect(bad).toEqual([]);
  });
  test("every pending entry still exists (remove it with its fix)", () => {
    const stale = Object.entries(KEYFRAME_PENDING).flatMap(([f, names]) =>
      names.filter((n) => !keyframes(cssSource(f)).some((k) => k.name === n)).map((n) => `${f} ${n}`),
    );
    expect(stale).toEqual([]);
  });
});

/** `--hud` is the chrome LINE colour (the accent on dark). As text it is `--hud-text`, which is
 *  `--accent-text` and holds AA for every preset; `color: var(--hud)` would bring back exactly the
 *  pale-accent text #1494 removed. */
describe("no text colour reads the raw --hud (#1531)", () => {
  const RAW_HUD = /(?<![-\w])color:[^;{}\n]*var\(\s*--hud\s*[,)]/g;
  test("in stylesheets", () => {
    expect(findings(CSS, cssSource, RAW_HUD).map(({ at }) => at)).toEqual([]);
  });
  test("in inline styles set from code", () => {
    expect(findings(CODE, codeSource, RAW_HUD).map(({ at }) => at)).toEqual([]);
  });
});

/** The pages outside the SPA (#1531 Phase 12): the server templates, connect.html and share.html
 *  restate the palette today. REPORT-ONLY until their ledger rows turn `done`; then the row's
 *  literals fail through nightjarLedger.test.ts like any other done surface. */
describe("pages outside the SPA (report only until Phase 12)", () => {
  const REPO = resolve(process.cwd(), "..");
  const PAGES = [
    "src/agent_sessions/templates/login.html",
    "src/agent_sessions/templates/login_totp.html",
    "src/agent_sessions/templates/change_password.html",
    "src/agent_sessions/templates/link_approve.html",
    "web/connect.html",
    "web/share.html",
  ];
  test("each page is readable and its colour literals are counted", () => {
    const report = PAGES.map((p) => {
      const src = readFileSync(join(REPO, p), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
      const n = (src.match(/#[0-9a-fA-F]{3,8}(?![\w-])|\brgba?\(/g) ?? []).length;
      return `${p}: ${n} colour literal(s)`;
    });
    console.info(`[nightjar] pages outside the SPA:\n  ${report.join("\n  ")}`);
    expect(report).toHaveLength(PAGES.length);
  });
});

// ---------------------------------------------------------------------------------------------------
// Every class a component reads from a CSS module exists in it (#1531)
// ---------------------------------------------------------------------------------------------------

/** A CSS module that loses a rule still compiles: `styles.optRow` is just `undefined`, the element
 *  gets no class, and the layout quietly collapses (it happened once in the NIGHTJAR rewrite of the
 *  Hand off sheet). So: for every `import x from "./y.module.css"`, every `x.name` / `x["name"]`
 *  read in that file must be a class the module defines. */
function moduleClasses(cssFile: string): Set<string> {
  const src = read(cssFile).replace(/\/\*[\s\S]*?\*\//g, "");
  const names = new Set<string>();
  // Selectors only (outside declaration blocks): strip `{…}` bodies, then collect `.name`.
  const selectors = src.replace(/\{[^{}]*\}/g, "{}").replace(/\{[^{}]*\}/g, "{}");
  for (const m of selectors.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)) names.add(m[1]);
  return names;
}

/** Classes read from a module that has never defined them (verified against `main` before #1531):
 *  hooks a test or a parent selector keys on, harmless as `undefined`. Listed so a NEW missing class
 *  still fails; remove an entry when its class gets a rule or its read goes away. */
const PREEXISTING_HOOKS = new Set([
  "components/chat/ChatPane.tsx:styles.turn",
  "components/structured/Outbox.tsx:chat.turn",
  "components/structured/StructuredPane.tsx:chat.turn",
  "routes/AppearanceApi.tsx:chat.turn",
  "components/playbooks/ProjectPlaybook.tsx:styles.invalid",
  "components/pulse/ActionRow.tsx:styles.age",
  "components/pulse/MissionConsole.tsx:styles.paneInner",
  "components/structured/Markdown.tsx:styles.lang",
  "components/structured/NotSent.tsx:styles.reason",
  "components/wizard/WizardShell.tsx:styles.stepLabel",
  "routes/Settings.tsx:styles.navGroup",
  "routes/Settings.tsx:styles.indexGroup",
]);

describe("every CSS-module class a component reads is defined (#1531)", () => {
  test("the checker sees what it is for", () => {
    expect([...moduleClasses("components/HudDialog.module.css")]).toContain("dialog");
  });
  test("in every component", () => {
    const missing: string[] = [];
    for (const f of CODE) {
      const full = codeSource(f);
      // Reads only: drop the import lines themselves (`toast.module.css` is not `toast.module`).
      const src = full.replace(/^import[^\n]*$/gm, "");
      for (const imp of full.matchAll(/import\s+(\w+)\s+from\s+["'](\.[^"']+\.module\.css)["']/g)) {
        const [, local, rel] = imp;
        // A local of the same name (a `d` date in a file that imports `d` styles) shadows it.
        const shadowed = new RegExp(
          `(const|let|var)\\s+${local}\\b|\\(\\s*${local}\\s*[,)]|\\b${local}\\s*=>|,\\s*${local}\\s*[,)]`,
        ).test(src);
        if (shadowed) continue;
        const cssFile = relative(SRC, resolve(SRC, f, "..", rel)).split("\\").join("/");
        const defined = moduleClasses(cssFile);
        const uses = [
          ...src.matchAll(new RegExp(`\\b${local}\\.([A-Za-z_][\\w]*)`, "g")),
          ...src.matchAll(new RegExp(`\\b${local}\\[["']([\\w-]+)["']\\]`, "g")),
        ].map((m) => m[1]);
        for (const u of new Set(uses))
          if (!defined.has(u) && !PREEXISTING_HOOKS.has(`${f}:${local}.${u}`))
            missing.push(`${f}: ${local}.${u} (${cssFile})`);
      }
    }
    expect(missing).toEqual([]);
  });
});

// ---- a CSS module's animation names resolve (#1531, Hermes on #1560) -----------------------
// CSS modules scope an animation NAME like a class: `animation: nj-sheet-up …` in a `.module.css`
// compiles to `_nj-sheet-up_<hash>`, a keyframe that does not exist, so the rule silently animates
// nothing. Sixteen NIGHTJAR rules (every phone sheet's slide-up, the dialogs' scan, toasts, the
// boot fade-out, the HQ caret) shipped that way. A keyframe defined in a GLOBAL stylesheet is named
// from a module as `global(name)`; one defined in the module itself stays local.

/** The animation names a stylesheet's `animation` / `animation-name` declarations use. */
function animationNames(css: string): { name: string; wrapped: boolean; line: number }[] {
  const out: { name: string; wrapped: boolean; line: number }[] = [];
  const re = /\banimation(?:-name)?\s*:\s*([^;}]+)/g;
  for (const m of css.matchAll(re)) {
    const line = css.slice(0, m.index).split("\n").length;
    for (const part of m[1].split(",")) {
      const tok = part.trim().match(/^(global\()?\s*([A-Za-z_-][\w-]*)\s*\)?/);
      if (!tok || ["none", "inherit", "initial", "unset", "var"].includes(tok[2])) continue;
      out.push({ name: tok[2], wrapped: Boolean(tok[1]), line });
    }
  }
  return out;
}

const GLOBAL_KEYFRAMES = new Set(
  CSS.filter((f) => !f.endsWith(".module.css")).flatMap((f) => keyframes(cssSource(f)).map((k) => k.name)),
);

describe("a CSS module names a global keyframe as global(name) (#1531)", () => {
  test("the checker sees through a bare name and accepts a wrapped one", () => {
    expect(animationNames("a { animation: nj-sheet-up 1s both; }")[0]).toMatchObject({ name: "nj-sheet-up", wrapped: false });
    expect(animationNames("a { animation: global(nj-sheet-up) 1s; }")[0]).toMatchObject({ name: "nj-sheet-up", wrapped: true });
    expect(GLOBAL_KEYFRAMES.has("nj-sheet-up")).toBe(true);
  });
  test("no module uses a global keyframe by its bare (module-scoped) name", () => {
    const bad = CSS.filter((f) => f.endsWith(".module.css")).flatMap((f) =>
      animationNames(cssSource(f))
        .filter((a) => !a.wrapped && GLOBAL_KEYFRAMES.has(a.name))
        .map((a) => `${f}:${a.line} ${a.name}`),
    );
    expect(bad).toEqual([]);
  });
});
