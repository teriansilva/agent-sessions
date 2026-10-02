// Is a font face actually present on THIS device? (#866)
//
// Two things this module exists to get right, both measured rather than assumed:
//
// 1. **Ask about the PRIMARY family, never the stack.** `"Cascadia Mono", ui-monospace, …,
//    monospace` is always "available", because `monospace` resolves everywhere. A stack-level
//    check therefore reports every preset as installed, and the greyed-out card state becomes a
//    lie. Callers pass the bare family name.
//
// 2. **`document.fonts.check()` cannot answer this.** It is the obvious API and it is the wrong
//    one: measured in headless Chromium on 2026-08-28, `document.fonts.check('12px "Definitely
//    Not A Font"')` returns **true**. It reports whether the FontFaceSet has everything it needs
//    loaded — for a name it has never heard of, nothing is pending, so the answer is yes. It is
//    not a first pass, not even a cheap one; it is a constant `true`. So the only mechanism here
//    is width comparison.
//
// The comparison uses SEVERAL fallback bases, and that is not belt-and-braces either: on a stock
// Linux box `monospace` IS DejaVu Sans Mono, so measuring "DejaVu Sans Mono, monospace" against
// "monospace" yields a difference of exactly 0 — a false negative for a font that is installed.
// Against `sans-serif` the same font differs by ~166 px at the probe size. A face counts as
// present when it moves the width away from ANY base.

/** The measuring surface — just the two members of CanvasRenderingContext2D this needs, so a
 *  test can supply a deterministic fake. jsdom has no canvas, so without this seam the
 *  behaviour below could only be tested in a browser. */
export interface TextMeasurer {
  font: string;
  measureText(text: string): { width: number };
}

/** Mixed advance widths (wide, narrow, digits, symbols) so two different faces are very unlikely
 *  to agree by accident, repeated to make any per-glyph difference add up. */
const PROBE = "mmmmmmmmmmwwwwwwlli0O1WW@#";
const PROBE_PX = 72;

/** Three genuinely different defaults. `monospace` alone is not enough (see the header). */
const BASES = ["monospace", "sans-serif", "serif"] as const;

/** Sub-pixel noise floor. Real differences at 72 px are in the tens-to-hundreds of pixels, so
 *  this is nowhere near a threshold anything meaningful can hide under. */
const EPSILON = 0.5;

let sharedCtx: TextMeasurer | null | undefined;

function defaultMeasurer(): TextMeasurer | null {
  if (sharedCtx !== undefined) return sharedCtx;
  try {
    sharedCtx = document.createElement("canvas").getContext("2d");
  } catch {
    sharedCtx = null; // no canvas (jsdom, a hardened embedding) — callers get "unknown"
  }
  return sharedCtx ?? null;
}

/** Quote a family name for a CSS font shorthand. Only used for names we control (the preset
 *  table); a name carrying a double quote would be nonsense here, so it is dropped rather than
 *  escaped — this string goes into `ctx.font`, and a broken shorthand is silently ignored by
 *  the canvas, which would make every answer "unavailable". */

/** Quote a family name for a CSS font shorthand. Only used for names we control (the preset
 *  table); a name carrying a double quote would be nonsense here, so it is dropped rather than
 *  escaped — this string goes into `ctx.font`, and a broken shorthand is silently ignored by
 *  the canvas, which would make every answer "unavailable".
 *
 *  Quoting is also what makes a MISUSE fail closed: hand this function a whole stack instead of
 *  a family and it becomes one absurd quoted name that resolves to nothing, i.e. "unavailable",
 *  rather than silently answering yes because the stack's own `monospace` tail resolved. */
function quoted(family: string): string {
  return `"${family.replace(/["\\]/g, "")}"`;
}

const cache = new Map<string, boolean>();

/** True when `family` resolves to a real face on this device.
 *
 *  Returns **true** when it cannot tell (no canvas available): an unavailable-looking card that
 *  is actually fine is a worse failure than the reverse — it hides a working choice — and the
 *  fallback in every preset stack means picking a truly-missing face is harmless anyway.
 *
 *  Results are cached per family: Settings renders one card per preset and re-renders on every
 *  keystroke in the custom field, and the answer cannot change without a page reload. */
export function isFontAvailable(
  family: string,
  measurer?: TextMeasurer,
): boolean {
  const name = family.trim();
  if (!name) return false;

  const ctx = measurer ?? defaultMeasurer();
  if (!ctx) return true; // cannot measure → do not claim it is missing

  if (!measurer) {
    const hit = cache.get(name);
    if (hit !== undefined) return hit;
  }

  const width = (font: string) => {
    ctx.font = `${PROBE_PX}px ${font}`;
    return ctx.measureText(PROBE).width;
  };

  let present = false;
  for (const base of BASES) {
    const baseline = width(base);
    if (Math.abs(width(`${quoted(name)}, ${base}`) - baseline) > EPSILON) {
      present = true;
      break;
    }
  }

  if (!measurer) cache.set(name, present);
  return present;
}

/** Test seam: drop the per-family cache (and the shared canvas). */
export function resetFontAvailability(): void {
  cache.clear();
  sharedCtx = undefined;
}
