// What the dashboard's summary numbers MEAN (#1123). Pure, so the rules are tested without a page.

import { quotaReadable, worstWindow } from "../../lib/agentUsage";
import type {
  AgentUsageRow,
  DashboardBand,
  MissionListRow,
} from "../../types/api";

/** The strip's one quota number: the LOWEST plan quota left, and nothing else.
 *
 *  Only `plan` rows enter it — the agent's own quota, a real percentage with a real reset. A
 *  `tokens` count means nothing without the operator's limit, a `manual` counter is not a
 *  percentage, and `none` is "not measured", never 0 %. So none of those is comparable, and none
 *  may be averaged or ranked against a plan window (review 75977).
 *
 *  Only rows the quota tile lists enter it (`quotaReadable`): a stale or refused quota is left out
 *  of both, so the strip never names an agent the tile below does not show. A figure that is
 *  current but came with a probe error (codex's rollout fallback) still counts, and the summary
 *  carries that. Ties go to the fresher figure. `null` when no agent reports a readable plan
 *  quota: the strip says so rather than inventing a number. */
export interface PlanSummary {
  engine: string;
  left: number;
  label: string;
  resets_at: number | null;
  stale: boolean;
  at: number;
}

export function lowestPlanLeft(rows: AgentUsageRow[]): PlanSummary | null {
  let best: PlanSummary | null = null;
  for (const r of rows) {
    if (r.source !== "plan" || !quotaReadable(r)) continue;
    const w = worstWindow(r);
    if (!w || !Number.isFinite(w.used_pct)) continue;
    const cand: PlanSummary = {
      engine: r.engine,
      left: Math.max(0, Math.min(100, 100 - w.used_pct)),
      label: w.label,
      resets_at: w.resets_at,
      stale: Boolean(r.stale || r.error),
      at: r.at,
    };
    if (
      best === null ||
      cand.left < best.left ||
      (cand.left === best.left && cand.at > best.at)
    ) {
      best = cand;
    }
  }
  return best;
}

/** Mission states the dashboard calls ACTIVE. `review` is counted apart: it waits on you. */
export const ACTIVE_MISSION_STATES = ["dispatching", "running"] as const;

export function isActiveMission(m: Pick<MissionListRow, "state">): boolean {
  return (ACTIVE_MISSION_STATES as readonly string[]).includes(m.state);
}

export const BAND_LABEL: Record<DashboardBand, string> = {
  needs_you: "Needs you",
  in_flight: "In flight",
  recently_active: "Recently active",
  idle: "Idle",
};

/** When a quota window resets. It is in the FUTURE, which `relTime` cannot say ("just now"). */
export function resetsAt(epoch: number): string {
  return new Date(epoch * 1000).toLocaleString(undefined, {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** How long until `epoch`, coarsely: a forecast is a pace extrapolated, so minutes would claim
 *  a precision it does not have. */
export function untilText(seconds: number): string {
  const h = seconds / 3600;
  if (h < 1) return "under an hour";
  if (h < 48) return `~${Math.round(h)}h`;
  return `~${Math.round(h / 24)}d`;
}

export type ForecastTone = "over" | "warn" | "muted";

/** The quota row's forecast line, or null when there is nothing to say. `warn` for a pace that
 *  runs out before the reset (it has NOT happened — degraded, not down), `over` once it has. */
export function forecastLine(
  row: AgentUsageRow,
  now: number = Date.now() / 1000,
): { text: string; tone: ForecastTone } | null {
  const f = row.forecast;
  if (!f) return null;
  switch (f.state) {
    case "out":
      return {
        text: f.resets_at
          ? `out — back at the reset, ${resetsAt(f.resets_at)}`
          : "at the limit",
        tone: "over",
      };
    case "exhausts": {
      if (!f.runs_out_at) return null;
      const when = `runs out in ${untilText(f.runs_out_at - now)} (${resetsAt(f.runs_out_at)}) at this pace`;
      const gap = f.resets_at
        ? ` — ${untilText(f.resets_at - f.runs_out_at).replace("~", "")} before the reset`
        : "";
      return { text: when + gap, tone: "warn" };
    }
    case "ok":
      return {
        text:
          f.pct_at_reset !== null
            ? `on pace for ~${Math.round(f.pct_at_reset)}% at the reset`
            : "on pace to stay under the limit",
        tone: "muted",
      };
    default:
      return { text: "forecast after a few more readings", tone: "muted" };
  }
}
