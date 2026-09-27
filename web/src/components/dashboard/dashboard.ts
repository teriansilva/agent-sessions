// What the dashboard's summary numbers MEAN (#1123). Pure, so the rules are tested without a page.

import { worstWindow } from "../../lib/agentUsage";
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
 *  A STALE window still counts — the last good figure is still the best thing known — but the
 *  summary carries its staleness and age, so it is never passed off as current. Ties go to the
 *  fresher figure. `null` when no agent reports a plan quota: the strip says so rather than
 *  inventing a number. */
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
    if (r.source !== "plan") continue;
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
