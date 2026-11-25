// Session-expiry window for the connect page (#662). The relay owns the session timer
// and announces {deadline, ttl} in the paired frame; the 4-hour constant here is only
// the fallback when no deadline arrived, and mirrors the relay's production default
// (RELAY_SESSION_TTL_SECONDS = 14400). Single source for the window and the expiry
// copy so the storage window, countdown, and messages can't drift apart again.

export const SESSION_LIMIT_FALLBACK_MS = 4 * 60 * 60 * 1000;
export const SESSION_LIMIT_LABEL = "4-hour limit";

/** Absolute ms timestamp when the saved session becomes unusable. The relay owns the
 *  timer: a valid announced deadline is used verbatim — shorter OR longer than four
 *  hours — and the 4-hour constant applies only when no deadline arrived. */
export function sessionExpiryMs(nowMs: number, deadlineSec?: number): number {
  if (deadlineSec && deadlineSec > 0) return deadlineSec * 1000;
  return nowMs + SESSION_LIMIT_FALLBACK_MS;
}

export function sessionExpiredMessage(): string {
  return `session expired (${SESSION_LIMIT_LABEL})`;
}
