/** Shared mocks for the console's own reads (#878).
 *
 * `/pulse` is now MISSION CONTROL, so every spec that visits it touches `/api/missions` and, once
 * a mission is selected, the three per-mission reads. The specs that predate the console mostly
 * do not care about missions at all — they assert the bell, `ActionRow`, the filter chips or the
 * page's overflow — so they get an EMPTY mission list here and keep their own assertions
 * untouched. That is the point: a Tier-B spec whose assertion changes during this migration is a
 * red flag, not a migration.
 */
import type { Page } from "@playwright/test";

export const EMPTY_MISSIONS = {
  missions: [],
  total: 0,
  limit: 50,
  offset: 0,
  facets: { projects: [], states: [] },
  store_error: null,
};

/** Resolve the LIST response from the request's own query, so a spec can mock PAGES and SCOPES
 *  rather than one fixed answer. The console's pagination and its archived scope are both
 *  expressed purely in the query string, so a mock that ignores it cannot tell a second page
 *  from a first — which is exactly how a regression in either survived. */
export type MissionListResolver = (
  q: URLSearchParams,
) => unknown | Promise<unknown>;

export interface MissionMockOptions {
  /** The rail's list — a fixed body, or a resolver over the query. Defaults to empty. */
  missions?: unknown | MissionListResolver;
  /** `GET /api/missions/{id}` — the mission plus a page of events. */
  mission?: unknown;
  objectives?: unknown;
  context?: unknown;
}

/** Route every mission endpoint the console reads. Call BEFORE `page.goto`. */
export async function mockMissions(
  page: Page,
  opts: MissionMockOptions = {},
): Promise<void> {
  // ORDER MATTERS, and it is the opposite of what reads naturally: Playwright matches the MOST
  // RECENTLY REGISTERED route first. So the catch-all goes on FIRST and the specific patterns
  // LAST — registered the other way round, `**/api/missions**` swallows `/api/missions/{id}` and
  // every per-mission read silently returns the LIST shape, which is not an error anywhere, just
  // a console with no events, no objectives and no tabs.
  await page.route("**/api/missions**", async (r) => {
    const m = opts.missions;
    if (typeof m === "function") {
      const q = new URL(r.request().url()).searchParams;
      // AWAITED, so a resolver can hold a page open — which is how the cross-scope race is
      // driven deterministically rather than by hoping a request is still in flight.
      return r.fulfill({ json: await (m as MissionListResolver)(q) });
    }
    return r.fulfill({ json: m ?? EMPTY_MISSIONS });
  });
  await page.route("**/api/missions/*", (r) =>
    r.fulfill({
      json: opts.mission ?? { ...MISSION, events: [], events_next_seq: null },
    }),
  );
  await page.route("**/api/missions/*/context", (r) =>
    r.fulfill({
      json: opts.context ?? {
        id: "msn_1",
        project_id: "",
        cwd: "",
        sessions: [],
        git: null,
        git_error: null,
      },
    }),
  );
  await page.route("**/api/missions/*/objectives", (r) =>
    r.fulfill({ json: opts.objectives ?? { objectives: [] } }),
  );
}

/** A LIST row, exactly as `GET /api/missions` produces one — `session_keys`, and NO `sessions`.
 *
 *  Producer-faithful on purpose. The previous fixture gave every list mission a detail-only
 *  `sessions` array, which is what hid a P0: the console iterated it, and a real list row has
 *  never had one, so `/pulse` threw on any non-empty production list while every browser test
 *  stayed green. A fixture that is kinder than the producer tests nothing. */
export function missionRow(over: Record<string, unknown> = {}) {
  return {
    id: "msn_1",
    title: "Kimi transcript adapter",
    project_id: "agent-sessions",
    cwd: "/repo",
    state: "running",
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
    closed_at: null,
    archived_at: null,
    outcome: null,
    session_keys: [],
    ...over,
  };
}

/** The DETAIL shape, from `GET /api/missions/{id}` — this one does carry the roster. */
export const MISSION = {
  id: "msn_1",
  title: "Kimi transcript adapter",
  instruction: null,
  brief: null,
  project_id: "agent-sessions",
  cwd: "/repo",
  engine: null,
  engine_source: null,
  state: "running",
  playbook_id: null,
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
  closed_at: null,
  archived_at: null,
  archiving_at: null,
  unarchiving_at: null,
  outcome: null,
  sessions: [],
};

export function missionList(
  missions: unknown[],
  storeError: string | null = null,
  /** The digest of the ORDERED ids the page was sliced out of (#896 review 19). The server sends
   *  one on every page and a stitching client requires them to agree, so a fixture that omits it
   *  is a fixture the console must — correctly — treat as unprovable. Default `"snap"`: one
   *  quiet snapshot, which is what almost every test means. A test about tearing passes a
   *  different value for the pages that came from a different list. */
  snapshot: string | null = "snap",
) {
  return {
    missions,
    total: missions.length,
    limit: 50,
    offset: 0,
    facets: { projects: [], states: [] },
    store_error: storeError,
    snapshot,
  };
}
