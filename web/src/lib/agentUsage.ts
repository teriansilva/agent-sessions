// How one agent's usage reads on screen (#839).
//
// Pure, and separate from the panel, because the interesting decisions here are not rendering
// ones: what a percentage MEANS differs per source, and a number whose meaning is wrong is worse
// than no number. Keeping them out of the component is what makes them testable.

import type { AgentUsageRow } from "../types/api";

/** Compact token count: 9.6M, 104M, 723k. Tokens run to nine figures, and a raw
 *  `104391103` in a table cell is unreadable at a glance. */
export function shortTokens(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "—";
  const scaled = (v: number, unit: string) =>
    `${v.toFixed(v < 10 ? 1 : 0).replace(/\.0$/, "")}${unit}`;
  if (n >= 1e9) return scaled(n / 1e9, "B");
  if (n >= 1e6) return scaled(n / 1e6, "M");
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`;
  return String(Math.round(n));
}

/** The tokens a limit is compared against: input + output, cache reads excluded.
 *  Mirrors `agent_usage.billable` server-side — the panel must not show one total while the
 *  threshold tests another. */
export function billable(row: AgentUsageRow): number {
  if (row.source === "manual") return row.manual_used || 0;
  const t = row.tokens;
  if (!t) return 0;
  return (t.in || 0) + (t.out || 0);
}

/** The window a `plan` row is actually judged on: the one nearest its limit. A session at 5%
 *  while the week sits at 96% is not at 5%. */
export function worstWindow(row: AgentUsageRow) {
  const ws = row.windows || [];
  if (!ws.length) return null;
  return ws.reduce((a, b) => (b.used_pct > a.used_pct ? b : a));
}

export type UsageTone = "ok" | "warn" | "over" | "none";

/** The row's status colour, which is load-bearing here and not decoration: it is the only
 *  thing that distinguishes "you have room" from "you are about to be cut off" at a glance.
 *  `none` is its own tone, never a green zero — an agent nobody has measured has not "used 0%". */
export function tone(pct: number | null, threshold: number): UsageTone {
  if (pct === null || !Number.isFinite(pct)) return "none";
  if (pct >= 100) return "over";
  if (pct >= threshold) return "warn";
  return "ok";
}

/** One line of plain English under the meter. Says which SOURCE the number came from, always:
 *  a manual counter the operator typed and a quota the agent reported are not the same claim,
 *  and a UI that renders them identically is lying by omission. */
export function usageCaption(row: AgentUsageRow): string {
  switch (row.source) {
    case "plan": {
      const w = worstWindow(row);
      if (!w) return "reported by the agent";
      const resets = w.resets_at
        ? ` · resets ${new Date(w.resets_at * 1000).toLocaleString(undefined, {
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
          })}`
        : "";
      return `${w.label}${resets}${row.plan ? ` · ${row.plan} plan` : ""}`;
    }
    case "tokens": {
      const used = shortTokens(billable(row));
      const days = row.window_days ? ` · last ${row.window_days} days` : "";
      return row.limit_tokens
        ? `${used} of ${shortTokens(row.limit_tokens)} tokens${days}`
        : `${used} tokens${days} · set a limit to track it`;
    }
    case "manual": {
      const used = shortTokens(row.manual_used || 0);
      return row.limit_tokens
        ? `${used} of ${shortTokens(row.limit_tokens)} tokens · counted by you`
        : `${used} tokens · counted by you`;
    }
    default:
      return "this agent reports no usage — set a limit and a count to track it";
  }
}

/** Why a figure is not current, or "" when it is. Kept distinct from an outright failure: a
 *  stale number is still a number, and blanking the panel because one probe timed out would
 *  throw away the last thing we know. */
export function stalenessNote(
  row: AgentUsageRow,
  now: number = Date.now() / 1000,
): string {
  if (row.source === "none" || row.source === "manual") return "";
  if (!row.at) return "not asked yet";
  const mins = Math.max(0, Math.round((now - row.at) / 60));
  const age = mins < 60 ? `${mins}m ago` : `${Math.round(mins / 60)}h ago`;
  if (row.error) return `last good figures, ${age} — ${row.error}`;
  return row.stale ? `${age}` : "";
}
