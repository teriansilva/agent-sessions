import { api } from "../../lib/api";
import type {
  AgentUsageResponse,
  DashboardSessions,
  MissionListRow,
  Session,
} from "../../types/api";

/** The dashboard's reads (#1123). Each tile polls its own source at the cadence the source costs:
 *  the session read reuses the list's TTL-cached scan and probe, missions read a local store, and
 *  quota is served from the usage CACHE — the page never triggers a vendor probe; only the
 *  operator's Refresh does. */
export const SESSIONS_POLL_MS = 30_000;
export const MISSIONS_POLL_MS = 60_000;
export const QUOTA_POLL_MS = 300_000;

export const LIVE_PREVIEW = 5;
export const RECENT_PREVIEW = 6;
export const MISSION_PREVIEW = 5;

/** An answer that is not the shape the tile draws is a failed read, never "nothing" — a server
 *  that predates the route, or a proxy error page, must not render as "0 running". */
function malformed(what: string): never {
  throw new Error(`The ${what} answer was not readable.`);
}

export async function readSessions(): Promise<DashboardSessions> {
  const out = await api.dashboardSessions(LIVE_PREVIEW, RECENT_PREVIEW);
  const live = out?.live as { health?: string; rows?: unknown } | undefined;
  if (
    !live ||
    (live.health !== "unavailable" && !Array.isArray(live.rows)) ||
    !Array.isArray(out?.recent?.rows)
  ) {
    malformed("sessions");
  }
  return out;
}

export interface MissionsSnapshot {
  /** `dispatching` + `running`, newest activity first — the preview. */
  active: MissionListRow[];
  activeTotal: number;
  review: MissionListRow[];
  reviewTotal: number;
}

/** Three state-filtered reads of the SAME list the Missions section shows, so each total is that
 *  list's own `total`. A store that would not answer is an error, never "no missions". */
export async function readMissions(): Promise<MissionsSnapshot> {
  const [running, dispatching, review] = await Promise.all(
    (["running", "dispatching", "review"] as const).map((state) =>
      api.missions({ state, limit: MISSION_PREVIEW }),
    ),
  );
  for (const page of [running, dispatching, review]) {
    if (!Array.isArray(page?.missions) || typeof page.total !== "number")
      malformed("missions");
    if (page.store_error)
      throw new Error("The mission store could not be read.");
  }
  const active = [...running.missions, ...dispatching.missions]
    .sort((a, b) => b.updated_at - a.updated_at)
    .slice(0, MISSION_PREVIEW);
  return {
    active,
    activeTotal: running.total + dispatching.total,
    review: review.missions,
    reviewTotal: review.total,
  };
}

export async function readQuota(): Promise<AgentUsageResponse> {
  const out = await api.agentUsage();
  if (!Array.isArray(out?.agents)) malformed("quota");
  return out;
}

/** At most this many rows are listed, however many pages that takes — a runaway bound, far past
 *  any single operator's live agents. Past it the footer says so rather than claiming "all". */
export const RUNNING_LIST_MAX = 2000;

export async function readAllRunning(): Promise<{
  rows: Session[];
  total: number;
}> {
  const rows: Session[] = [];
  let offset: number | null = 0;
  let total = 0;
  while (offset !== null && rows.length < RUNNING_LIST_MAX) {
    const page = await api.runningSessions("live", 200, offset);
    if (page.running_filter_unavailable) {
      throw new Error("Couldn’t read which agents are running.");
    }
    rows.push(...page.sessions);
    total = page.total;
    offset = page.next_offset;
  }
  return { rows, total };
}
