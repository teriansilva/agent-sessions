// Terminal text size (#859). On a phone the xterm font size is not a cosmetic preference —
// it IS the column count the agent lays out against. At the shipped 13 px a 412 px phone
// gives the agent a 50-column terminal, and a TUI that lays out in columns (opencode above
// all) collapses: its label column squeezes to three characters and its right-hand value
// stacks one word per line. Dropping to 10 px buys 65 columns, 9 px buys 72.
//
// The size is deliberately NOT a field on TerminalTheme (where the 13 px used to live):
// two owners would let a theme switch silently reset the operator's zoom. One constant,
// one store, one place to change it.
//
// Mirror of the server contract in src/agent_sessions/prefs.py (coerce_term_font_size /
// TERM_FONT_SIZE_{MIN,MAX,DEFAULT}). The shared normalization fixture that pins both
// implementations against each other is tests/fixtures/term_font_size_cases.json.

export const TERM_FONT_SIZE_MIN = 8;
export const TERM_FONT_SIZE_MAX = 20;
export const DEFAULT_TERM_FONT_SIZE = 13; // keep in sync with prefs.py

export const TERM_SIZE_STORAGE_KEY = "tr-termsize";

/** Round half AWAY FROM ZERO, spelled as floor(x + 0.5) rather than Math.round.
 *
 *  Not a style choice: Python's `round()` is banker's rounding (`round(10.5) == 10`) while
 *  `Math.round(10.5) === 11`, so the two implementations would silently disagree on every
 *  half value. `floor(x + 0.5)` is what prefs.py spells too, and the domain here is positive,
 *  so the two agree by construction rather than by coincidence. */
function roundHalfUp(n: number): number {
  return Math.floor(n + 0.5);
}

/** Narrow any value to a usable terminal font size — the READ boundary, and it is lenient
 *  by design (the write boundary, POST /api/prefs, is strict and 422s instead).
 *
 *  A numeric value is rounded then clamped, so neither an edited localStorage nor a
 *  hand-edited prefs.json can strand the terminal at 2 px. Anything that isn't a number —
 *  including a *boolean*, which `typeof` would let through as truthy arithmetic — falls back
 *  to the default. Never throws. */
export function coerceTermFontSize(v: unknown): number {
  if (typeof v !== "number" || !Number.isFinite(v))
    return DEFAULT_TERM_FONT_SIZE;
  const n = roundHalfUp(v);
  return Math.min(TERM_FONT_SIZE_MAX, Math.max(TERM_FONT_SIZE_MIN, n));
}

/** True only for a value already in canonical form — an integer inside the range. Used to
 *  decide whether a *device* has an explicit choice worth preferring over the server's
 *  (mirrors `normalizeAccent(...) !== null` in AccentProvider's seed check). */
export function isTermFontSize(v: unknown): v is number {
  return (
    typeof v === "number" &&
    Number.isInteger(v) &&
    v >= TERM_FONT_SIZE_MIN &&
    v <= TERM_FONT_SIZE_MAX
  );
}

/** Parse a localStorage string into a number before coercing. Storage is string-typed, so
 *  the raw read is `"10"`, which `coerceTermFontSize` (correctly) rejects as a non-number:
 *  the parse belongs here, at the storage boundary, not inside the shared coercion. */
function parseStored(raw: string | null): unknown {
  if (raw === null || raw.trim() === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

/** The size cached on this device, or the default. Never throws. */
export function readStoredTermFontSize(): number {
  try {
    return coerceTermFontSize(
      parseStored(localStorage.getItem(TERM_SIZE_STORAGE_KEY)),
    );
  } catch {
    return DEFAULT_TERM_FONT_SIZE;
  }
}

/** True when this device has made an explicit, still-valid choice — the gate for the
 *  one-time server seed (a stale server value must not override it on every reload). */
export function hasStoredTermFontSize(): boolean {
  try {
    return isTermFontSize(
      parseStored(localStorage.getItem(TERM_SIZE_STORAGE_KEY)),
    );
  } catch {
    return false; // storage disabled — treat as no local choice; the seed is then this run only
  }
}

export function storeTermFontSize(size: number): void {
  try {
    localStorage.setItem(
      TERM_SIZE_STORAGE_KEY,
      String(coerceTermFontSize(size)),
    );
  } catch {
    /* storage disabled — the size still applies for this page */
  }
}

/** One step of the stepper, clamped. Returned rather than applied so the caller can also
 *  use it to decide whether the button is at its end (`step(s, -1) === s` → disabled). */
export function stepTermFontSize(size: number, delta: number): number {
  return coerceTermFontSize(coerceTermFontSize(size) + delta);
}
