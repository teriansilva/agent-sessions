// Terminal font FAMILY (#866) — the second axis beside the size (#859), and it is an axis for
// exactly the same reason. The face used to be a field on `TerminalTheme`, which gave one value
// two owners: flipping dark→light re-applied the theme's face and would silently undo an
// operator's choice. `fontSize` was moved out for that reason; the family follows it here, so
// "one value, one owner" is structural rather than a convention someone has to remember.
//
// The face is engine-agnostic on purpose. claude, opencode, codex, gemini, antigravity, kimi and
// a plain shell all render through ONE xterm, so this single value covers every agent — there is
// deliberately no per-engine and no per-session override.
//
// Mirror of the server contract in src/agent_sessions/prefs.py
// (coerce_term_font_family / is_valid_term_font_family / DEFAULT_TERM_FONT_FAMILY). The shared
// fixture that pins both implementations against each other is
// tests/fixtures/term_font_family_cases.json.

/** The stack the terminal has always used. Keep byte-identical to prefs.py's
 *  DEFAULT_TERM_FONT_FAMILY: it is what a device with no choice is seeded with, and a drift
 *  would show up as "the System card isn't selected even though nothing was changed". */
export const DEFAULT_TERM_FONT_FAMILY =
  "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";

export const TERM_FONT_FAMILY_MAX_LEN = 120;

export const TERM_FONT_STORAGE_KEY = "tr-termfont";

/** The value is interpolated into a CSS declaration and into xterm's own font strings, so the
 *  charset is an ALLOWLIST, not a denylist: letters, digits, space, comma, hyphen, underscore,
 *  period and the two quote marks. No `;` `{` `}` `(` `)` `<` `>` `\` and no newlines — which is
 *  what makes `url(…)`, a second declaration and a tag escape unreachable rather than unlikely. */
const STACK_CHARS = /^[A-Za-z0-9 ,._'"-]+$/;

/** CSS-wide keywords, plus `default`. Chromium rejects every one of these as an item in a
 *  font-family LIST (measured), and `font-family: inherit` on its own would make the terminal
 *  inherit the app chrome's face — which is not a font choice at all. Rejected everywhere. */
const CSS_WIDE = new Set([
  "inherit",
  "initial",
  "unset",
  "revert",
  "revert-layer",
  "default",
]);

/** One CSS identifier: starts with a letter or underscore (optionally after a single hyphen,
 *  which is what makes `-apple-system` legal), then letters, digits, hyphens, underscores.
 *
 *  A leading DIGIT is the case that matters: `123` and `1Password` are not identifiers, and
 *  Chromium rejects the whole declaration rather than the one family — so `123, monospace`
 *  leaves the terminal on its previous face while the UI claims the new stack is active. A
 *  period is absent on purpose: `Font.Name` is invalid unquoted (measured) and must be quoted. */
const IDENT = /^-?[A-Za-z_][A-Za-z0-9_-]*$/;

/** CSS generic families. A generic is a legal family on its own (`monospace`), but it may not
 *  START a multi-token family name: Chromium consumes it as a generic and then rejects the whole
 *  declaration on the trailing tokens, so `serif foo, monospace` is discarded entirely while the
 *  pre-fix validator called it fine — Settings stored it and the terminal kept the old face.
 *  Measured case-insensitively over a 1712-case fuzz corpus: `Serif A1` fails for the same
 *  reason `serif foo` does.
 *
 *  A generic in any LATER position stays legal, and that restraint matters: `PT Serif` and
 *  `Noto Sans Mono` are real font names, so rejecting them would be a false refusal with a
 *  real cost.
 *
 *  The set is the full CSS Fonts 4 list, deliberately WIDER than what today's Chromium rejects
 *  (it accepts `ui-monospace foo`, not having shipped that generic). Covering the whole set keeps
 *  "everything we accept, the browser accepts" true under a future browser too, and costs
 *  nothing real — no font is named "ui-rounded Something". */
const CSS_GENERIC = new Set([
  "serif",
  "sans-serif",
  "cursive",
  "fantasy",
  "monospace",
  "system-ui",
  "math",
  "emoji",
  "fangsong",
  "ui-serif",
  "ui-sans-serif",
  "ui-monospace",
  "ui-rounded",
]);

/** True for a stack that is permitted, usable, AND actually parses as CSS.
 *
 *  The relationship to the browser's own parser is deliberately ONE-directional: everything this
 *  accepts, Chromium accepts (pinned by a real-browser test). The converse does not hold, and
 *  should not — Chromium accepts `"Fira Code` by auto-closing the string, and accepts `--weird`
 *  as a family name. Both are typos in this context, and silently storing a face the operator
 *  did not mean is worse than refusing it.
 *
 *  Three classes are rejected, and each was a real defect before it was:
 *   - **charset** (`;`, `{}`, `()`, `<>`, `\`, newlines) — the injection boundary;
 *   - **structure** (`Menlo,,monospace`, `,monospace`, `"Fira Code`, whitespace only) — all-legal
 *     characters, still renders as nothing;
 *   - **grammar** (`123, monospace`, `Font.Name`, `inherit`) — all-legal characters, valid
 *     structure, and the browser throws the whole declaration away, leaving the UI and the
 *     terminal disagreeing about which face is live. This is the class Hermes caught in #868. */
function stackIsSane(stack: string): boolean {
  if (!stack || stack.length > TERM_FONT_FAMILY_MAX_LEN) return false;
  if (!STACK_CHARS.test(stack)) return false;
  for (const raw of stack.split(",")) {
    const seg = raw.trim();
    if (!seg) return false; // empty segment: "a,,b", ",b", "b,"
    const q = seg[0];
    if (q === '"' || q === "'") {
      // A quoted family may contain anything the charset allows — digits, periods, spaces —
      // but must be closed by the same mark and hold something.
      if (seg[seg.length - 1] !== q || seg.length < 3 || seg.slice(1, -1).includes(q))
        return false;
      continue;
    }
    if (seg.includes('"') || seg.includes("'")) return false; // unbalanced quote
    // Unquoted: a sequence of CSS identifiers separated by whitespace ("Segoe UI Mono").
    const words = seg.split(/\s+/);
    for (const w of words) {
      if (!IDENT.test(w) || CSS_WIDE.has(w.toLowerCase())) return false;
    }
    if (words.length > 1 && CSS_GENERIC.has(words[0].toLowerCase())) return false;
  }
  return true;
}

/** Narrow any value to a usable font stack — the READ boundary, lenient by design (the write
 *  boundary, POST /api/prefs, is the strict one and 422s instead).
 *
 *  Whitespace is trimmed and nothing else is normalized. That restraint is load-bearing: the UI
 *  decides which preset card reads as ACTIVE by comparing this string to its preset stacks, so
 *  any rewriting here (collapsing spaces, re-joining with ", ") would make a preset show up as
 *  "Custom" on the next device that seeds from the server. Never throws. */
export function coerceTermFontFamily(v: unknown): string {
  if (typeof v !== "string") return DEFAULT_TERM_FONT_FAMILY;
  const s = v.trim();
  return stackIsSane(s) ? s : DEFAULT_TERM_FONT_FAMILY;
}

/** True only for a value already in canonical form — used to decide whether a *device* has an
 *  explicit choice worth preferring over the server's (mirrors `isTermFontSize`). Untrimmed
 *  whitespace does not count as canonical: the store always writes the trimmed form. */
export function isTermFontFamily(v: unknown): v is string {
  return typeof v === "string" && v === v.trim() && stackIsSane(v);
}

/** The stack cached on this device, or the default. Never throws. */
export function readStoredTermFontFamily(): string {
  try {
    return coerceTermFontFamily(localStorage.getItem(TERM_FONT_STORAGE_KEY));
  } catch {
    return DEFAULT_TERM_FONT_FAMILY;
  }
}

/** True when this device has made an explicit, still-valid choice — the gate for the one-time
 *  server seed (a stale server value must not override it on every reload). */
export function hasStoredTermFontFamily(): boolean {
  try {
    return isTermFontFamily(localStorage.getItem(TERM_FONT_STORAGE_KEY));
  } catch {
    return false; // storage disabled — treat as no local choice; the seed is then this run only
  }
}

export function storeTermFontFamily(family: string): void {
  try {
    localStorage.setItem(
      TERM_FONT_STORAGE_KEY,
      coerceTermFontFamily(family),
    );
  } catch {
    /* storage disabled — the face still applies for this page */
  }
}

/** A selectable face.
 *
 *  `primary` is NOT decoration and NOT derivable by parsing `stack`: availability is asked about
 *  the primary family ALONE. Asking whether `"Cascadia Mono", ui-monospace, …, monospace` is
 *  available always answers yes, because `monospace` resolves everywhere — so a stack-level
 *  check would report every preset as installed and the greyed-out state would be a lie.
 *  `primary: null` means "nothing to check": the System preset is whatever the device ships, so
 *  it is available by definition. */
export interface TermFontPreset {
  id: string;
  label: string;
  /** What gets stored and handed to xterm. */
  stack: string;
  /** The family whose presence decides the card's availability, or null for "always". */
  primary: string | null;
  /** One short line under the specimen. */
  note: string;
}

/** The preset list. Deliberately small, and deliberately WITHOUT a bundled face: Phase 1 ships
 *  no assets, so no card may offer a face this build doesn't carry (#866 Phase 1). Every entry
 *  ends in the system stack, so a preset always degrades to something readable rather than to
 *  the browser's proportional default. */
export const TERM_FONT_PRESETS: readonly TermFontPreset[] = [
  {
    id: "system",
    label: "System",
    stack: DEFAULT_TERM_FONT_FAMILY,
    primary: null,
    note: "This device's default",
  },
  {
    id: "jetbrains",
    label: "JetBrains Mono",
    stack: `"JetBrains Mono", ${DEFAULT_TERM_FONT_FAMILY}`,
    primary: "JetBrains Mono",
    note: "Wide, high-contrast; box drawing",
  },
  {
    id: "fira",
    label: "Fira Code",
    stack: `"Fira Code", ${DEFAULT_TERM_FONT_FAMILY}`,
    primary: "Fira Code",
    note: "Ligatures render unligated here",
  },
  {
    id: "cascadia",
    label: "Cascadia Mono",
    stack: `"Cascadia Mono", ${DEFAULT_TERM_FONT_FAMILY}`,
    primary: "Cascadia Mono",
    note: "Ships with Windows Terminal",
  },
  {
    id: "sfmono",
    label: "SF Mono",
    stack: `"SF Mono", ${DEFAULT_TERM_FONT_FAMILY}`,
    primary: "SF Mono",
    note: "macOS / iOS",
  },
  {
    id: "roboto",
    label: "Roboto Mono",
    stack: `"Roboto Mono", ${DEFAULT_TERM_FONT_FAMILY}`,
    primary: "Roboto Mono",
    note: "Android's monospace",
  },
] as const;

/** The preset whose stack is exactly the current value, or undefined — which is precisely what
 *  "Custom" means, so the caller needs no separate flag. */
export function presetForStack(stack: string): TermFontPreset | undefined {
  return TERM_FONT_PRESETS.find((p) => p.stack === stack);
}
