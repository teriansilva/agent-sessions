/** The automation queue's words (#2422): a step that launches a session waits for a free slot
 *  instead of failing, and capacity is kept for automations. Pure, so every surface (the
 *  Specialists pool line, Settings → Session limit, the Automations list and a run's detail)
 *  says the same thing. Queued is the idle grey (docs/design.md §7): waiting, never a failure. */
import type { RunQueue } from "../types/automations";

/** 1st, 2nd, 3rd, 4th … 11th, 12th, 13th … 21st. */
export function ordinal(n: number): string {
  const tens = n % 100;
  if (tens >= 11 && tens <= 13) return `${n}th`;
  switch (n % 10) {
    case 1:
      return `${n}st`;
    case 2:
      return `${n}nd`;
    case 3:
      return `${n}rd`;
    default:
      return `${n}th`;
  }
}

/** `21:14`, local time. */
export function hhmm(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

export const QUEUED_WORDS = "Queued for a free slot";

/** A queued run's row sub-line: `Queued for a free slot · since 21:14 · 2nd in line`. */
export function queueLine(q: RunQueue, clock: (ts: number) => string = hhmm): string {
  return `${QUEUED_WORDS} · since ${clock(q.since)} · ${ordinal(q.position)} in line`;
}

/** The detail panel's Outcome: `Queued for a free slot · 2nd in line`. */
export function queueOutcome(q: RunQueue): string {
  return `${QUEUED_WORDS} · ${ordinal(q.position)} in line`;
}

/** The notice body: what is full, in the server's words, and that nothing has failed. */
export function queueNoticeBody(q: RunQueue): string {
  const fallback =
    q.capacity === "concurrency"
      ? "the previous run of this automation is still working"
      : "every slot is taken";
  const what = (q.detail || fallback).replace(/[.\s]+$/, "");
  return `${what.charAt(0).toUpperCase()}${what.slice(1)}. This step starts on its own as soon as a slot frees; nothing has failed.`;
}

/** The Automations list header: `3 queued · waiting for a free slot`, or "" for an empty queue
 *  (nothing is shown then). */
export function queueHeader(length: number | undefined): string {
  return length && length > 0 ? `${length} queued · waiting for a free slot` : "";
}

/** The slots a reservation keeps: never more than the limit itself (`min(reserved, max)`). */
export function effectiveReserve(reserved: number, max: number): number {
  return Math.max(0, Math.min(reserved, max));
}

/** The reservation's one-line consequence. `where` is the surface: the specialist pool or the
 *  session limit. When the whole limit is reserved it says plainly that only automations start. */
export function reserveHelp(max: number, reserved: number, where: "pool" | "sessions"): string {
  const eff = effectiveReserve(reserved, max);
  if (eff >= max)
    return where === "pool"
      ? `Only automations can start here: the whole pool of ${max} is reserved for them.`
      : `Only automations can start sessions: all ${max} are reserved for them.`;
  return `Other starts can use ${max - eff} of ${max}; automations can use any free slot.`;
}

/** Why a reserve can't be saved against this limit, or "" when it can. The server stays the
 *  authority (it answers 422); this only keeps an obviously refused save from being sent. */
export function reserveProblem(max: number, reserved: number, limitWord: string): string {
  if (Number.isInteger(max) && Number.isInteger(reserved) && reserved > max)
    return `The reserve can't be more than the ${limitWord} (${max}).`;
  return "";
}
