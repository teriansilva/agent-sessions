import type { MemoryPressure } from "../types/api";

const GIB = 1024 ** 3;

/** Bytes as the GB the operator's limit is set in (binary, like systemd's `G`): one decimal below
 *  10 GB, whole numbers above, never a trailing ".0". */
export function gb(bytes: number): string {
  const value = bytes / GIB;
  const shown = value < 10 ? Math.round(value * 10) / 10 : Math.round(value);
  return String(shown);
}

/** The row chip (#1643), after a memory icon: "HIGH 6.4/8 GB" / "CRIT 7.6/8 GB". The level is in
 *  the words, not only the colour (Hermes on #1651): a throttled session reads critical with the
 *  same bytes. Short, because it must fit the narrowest sidebar's meta line. */
export function memoryChip(p: MemoryPressure): string {
  return `${p.level === "critical" ? "CRIT" : "HIGH"} ${gb(p.working_set)}/${gb(p.max)} GB`;
}

/** The chip's accessible name (#1643): the words a screen reader says for it. */
export function memoryLabel(p: MemoryPressure): string {
  return `Memory ${p.level}: ${gb(p.working_set)} of ${gb(p.max)} GB${isShared(p) ? ", shared group" : ""}`;
}

/** A group this session shares, capped outside BattleLab (#1643): its own limit setting won't
 *  help, so the copy says so and the pane offers no Resource limits shortcut. */
export function isShared(p: MemoryPressure): boolean {
  return p.scope === "shared";
}

/** The row chip's tooltip (#1643). */
export function memoryTitle(p: MemoryPressure): string {
  return isShared(p)
    ? `Memory ${p.level}: ${gb(p.working_set)} of ${gb(p.max)} GB in a group this session shares, ` +
        "limited outside BattleLab"
    : `Memory ${p.level}: ${gb(p.working_set)} of ${gb(p.max)} GB in use by this session’s agent ` +
        "and its tools";
}

/** Can the operator's own Resource limits setting help (#1643)? When the figures are this
 *  session's own, or this session is the one being throttled at its own limit. */
export function ownLimitHelps(p: MemoryPressure): boolean {
  // Not when the only throttling seen is a shared group's: raising this session's limit can't
  // lift that (Hermes on #1651).
  return p.throttled === "session" || (!isShared(p) && p.throttled !== "shared");
}

/** Close to a hard cap by the figures alone (90%, the server's critical ratio). */
const NEAR = 0.9;

/** The pane banner's sentence (#1643): two independent parts (Hermes on #1651).
 *  - Closeness, from the figures: the most urgent group's (`scope`), session or shared.
 *  - Throttling, only on evidence (`throttled`), named by WHOSE limit is holding it back — a
 *    shared group's throttling is never presented as this session's own, whichever ratio shows. */
export function memorySentence(p: MemoryPressure): string {
  const amount = `${gb(p.working_set)} of ${gb(p.max)} GB`;
  const stops = ` At ${gb(p.max)} GB it stops, with whatever it was doing unfinished.`;
  const near = p.working_set / p.max >= NEAR;
  const closeness = isShared(p)
    ? near
      ? `${amount} in a group this session shares — close to that group’s limit, set outside ` +
        `BattleLab.${stops}`
      : `${amount} in a group this session shares with other work, limited outside BattleLab.`
    : near
      ? `${amount} — close to this session’s limit.${stops}`
      : `${amount} in use by this session’s agent and the tools it started.`;
  const throttle =
    p.throttled === "session"
      ? " The kernel is holding this session back at its own limit, which Resource limits can raise."
      : p.throttled === "shared"
        ? " A group this session shares is being held back at its limit, set outside BattleLab; " +
          "raising this session’s own limit won’t help that."
        : isShared(p)
          ? " Raising this session’s own limit won’t help."
          : "";
  return closeness + throttle;
}
