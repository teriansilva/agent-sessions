/** The recent-work window (#1086): 1–3 days, default 1.
 *
 *  The same rule as `prefs.coerce_pulse_window_days` / `is_valid_pulse_window_days`, pinned by one
 *  shared table (tests/fixtures/pulse_window_days_cases.json) that both suites read. READ clamps and
 *  never throws — a stored 7 from the old 1–30 range is the longest window still offered, not a
 *  reset. WRITE is strict: the server answers 422 rather than coerce. Rounding is spelled
 *  `floor(x + 0.5)` on both sides, never `Math.round` vs `round()`, which disagree on halves. */
export const RECENT_WINDOW_MIN = 1;
export const RECENT_WINDOW_MAX = 3;
export const RECENT_WINDOW_DEFAULT = 1;

export function coerceRecentWindowDays(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return RECENT_WINDOW_DEFAULT;
  return Math.max(RECENT_WINDOW_MIN, Math.min(RECENT_WINDOW_MAX, Math.floor(value + 0.5)));
}

export function isRecentWindowDays(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= RECENT_WINDOW_MIN &&
    value <= RECENT_WINDOW_MAX
  );
}
