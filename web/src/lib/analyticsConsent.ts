/** Usage analytics consent (#1009): one save path for the setup wizard and Settings.
 *
 *  A failed save is NOT "off". A replay of setup can start from a stored `true`, so a failed untick
 *  leaves analytics on; and a write the server committed can still lose its response. So a failure
 *  is reconciled with an awaited, direct config read whose result and error are both inspected —
 *  not the ConfigContext refresh, which returns nothing and swallows its own failures. */
import { api } from "./api";
import { DOCS_HOME_URL } from "./links";
import type { AnalyticsState } from "../types/api";

/** The docs section that lists exactly what is sent. */
export const ANALYTICS_DOCS_URL = `${DOCS_HOME_URL}guide/settings#usage-analytics`;

export type ConsentSave =
  /** The server holds the chosen value — from the save's own response, or found on reconcile. */
  | { ok: true; state: AnalyticsState }
  /** It does not. `state` is what the server holds, or `null` when that could not be read. */
  | { ok: false; state: AnalyticsState | null };

export async function saveAnalyticsConsent(value: boolean): Promise<ConsentSave> {
  try {
    const r = await api.setAnalyticsConsent(value);
    return { ok: true, state: r.analytics };
  } catch {
    try {
      const state = (await api.config()).analytics ?? null;
      if (state && state.decided && state.enabled === value) {
        return { ok: true, state };
      }
      return { ok: false, state };
    } catch {
      return { ok: false, state: null };
    }
  }
}

/** The setting actually in effect, in words: "on", "off", or "not set, so off". */
export function settingInEffect(state: AnalyticsState): string {
  if (!state.decided) return "not set, so off";
  return state.enabled ? "on" : "off";
}

/** The error line after a failed save. */
export function consentSaveError(state: AnalyticsState | null): string {
  if (!state) {
    return "Couldn't save your choice, and the current setting couldn't be read.";
  }
  return `Couldn't save your choice — your previous setting (${settingInEffect(state)}) is still in effect.`;
}
