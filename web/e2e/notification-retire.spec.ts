import { expect, test, type Page } from "@playwright/test";

import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
} from "./mission-console";

/** The bell converges when an action is resolved (#800).
 *
 *  jsdom can dispatch the event and read a badge, but it cannot prove the two things that
 *  actually matter here, which is why this runs in a real browser at both widths:
 *
 *  * The bell has TWO mount paths — an anchored dropdown on desktop, a portalled drawer at
 *    ≤640px (#750) — and the empty state lives inside the panel. A unit test that only checks
 *    the closed badge never opens either one.
 *  * The convergence contract is deliberately asymmetric: the same tab updates immediately off
 *    a DOM event, other tabs wait for their next fetch. That is two browsing contexts, which
 *    jsdom does not have, so the "other tab must NOT jump" half would otherwise be unasserted —
 *    and it is the half that would silently pass if someone "fixed" this with a global store.
 *
 *  WHERE THE ACTION IS RESOLVED: the mission console. The session pane's decision strip, where
 *  these cases were driven from #948 P3 on, was removed in #1049; since then the console's
 *  `ActionRow` is the only Approve, and it renders a decision for a session its mission HOLDS.
 *  That is also the only kind of decision the server counts in the badge (#1057), so the fixture
 *  is a held one. The bell assertions are unchanged.
 */

const UUID = "abcabcab-0000-4000-8000-000000000800";
const MID = `msn_${"0800".repeat(8)}`;
const NOW = Math.floor(Date.now() / 1000);

const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
};

const ORCH_CONFIG = {
  enabled: true,
  autonomy: "suggest",
  confidence_min: 0.7,
  allowed_verbs: ["continue"],
  // The panel prints the enforced ceiling verbatim; omitting it throws inside render and the
  // whole route lands in the chunk error boundary with no failed request to point at.
  auto_verbs_ceiling: ["continue"],
  interval_minutes: 10,
  max_actions_per_pass: 4,
  proposal_ttl_minutes: 30,
  notify: "escalations",
  stale_hours: 24,
  nudge_template: "carry on",
  default_nudge_template: "carry on",
};

const ACTION = {
  id: "act-1",
  state: "proposed",
  projection: "actionable",
  can_approve: true,
  can_reject: true,
  verb: "continue",
  confidence: 0.9,
  rationale: "stopped mid-task",
  session_id: `claude:${UUID}`,
  engine: "claude",
  title: "finish the docs",
  project: "agent-sessions",
  project_id: "p1",
  evidence: "none",
  ts: NOW - 60,
  expires_at: NOW + 1800,
};

const NOTIFICATION = {
  id: "n1",
  title: "claude needs a decision",
  reason: "waiting on a menu choice",
  project: "agent-sessions",
  engine: "claude",
  session_id: `claude:${UUID}`,
  action_id: "act-1",
  ts: NOW - 60,
  read: false,
};

function card(open: boolean) {
  return {
    id: `claude:${UUID}`,
    engine: "claude",
    title: ACTION.title,
    cwd: "/home/u/agent-sessions",
    project: { kind: "project", id: "p1", name: "agent-sessions", color: "#ffb000" },
    state: open ? "needs_you" : "idle",
    live: false,
    last_activity: NOW - 600,
    last_mtime: NOW - 600,
    intervention_required: false,
    ai_summary: "",
    synthesis: "",
    mission_id: MID,
    ...(open ? { pending_action: ACTION } : {}),
  };
}

/** Wire one page against a server that retires the alert when the action is approved, with the
 *  mission holding the session open in the console. */
async function mockApp(page: Page, state: { open: boolean }) {
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  // The bell reads this on mount, on open, and every 60s. Driven by shared `state` so BOTH
  // pages see the same server: retiring is a server-side fact, and the only thing the custom
  // event changes is WHEN a given tab notices it.
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({
      json: state.open
        ? { notifications: [NOTIFICATION], unread: 1 }
        : { notifications: [], unread: 0 },
    }),
  );
  await page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    r.fulfill({
      json: {
        config: ORCH_CONFIG,
        pending: state.open ? [ACTION] : [],
        feed: [],
        expired_now: 0,
        delivering_verbs: ["continue", "choose", "answer"],
        running: [],
        last: {},
      },
    }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        cache_version: 2,
        generated_at: NOW,
        window_days: 3,
        scan_depth: "slow",
        input_fingerprint: null,
        synthesis_skipped: false,
        banner: null,
        cards: [card(state.open)],
      },
    }),
  );
  await mockMissions(page, {
    missions: missionList([
      missionRow({ id: MID, title: "Docs pass", session_keys: [ACTION.session_id] }),
    ]),
    mission: {
      ...MISSION,
      id: MID,
      title: "Docs pass",
      sessions: [{ session_key: ACTION.session_id, removed_at: null }],
      events: [],
      events_next_seq: null,
    },
  });
  // Approving settles the action AND retires its bell row — the server does both, which is
  // exactly why a tab that never re-fetches would keep showing a badge for neither.
  await page.route(/\/api\/pulse\/actions\/.*\/approve$/, (r) => {
    state.open = false;
    return r.fulfill({ json: { ...ACTION, state: "delivered" } });
  });
}

async function openConsole(page: Page) {
  await page.goto(`/mission?m=${MID}`);
  await expect(page.getByTestId("console-title")).toHaveText("Docs pass");
}

test("resolving the last action empties the bell — badge and panel — in the same tab", async ({
  page,
}) => {
  const state = { open: true };
  await mockApp(page, state);
  await openConsole(page);

  const bell = page.getByRole("button", { name: /^Notifications/ });
  await expect(bell).toHaveAttribute("aria-label", "Notifications, 1 unread");

  await page.getByRole("button", { name: /^approve$/i }).click();

  // The badge goes, without waiting out the 60s poll…
  await expect(bell).toHaveAttribute("aria-label", "Notifications");
  // …and the panel itself agrees. This is the half a closed-badge assertion cannot see, and it
  // exercises whichever of the two mount paths (dropdown / drawer) this width uses.
  await bell.click();
  await expect(page.getByText("Nothing needs you right now.")).toBeVisible();
});

test("another tab does not jump; it converges on its next fetch", async ({
  context,
  page,
}) => {
  const state = { open: true };
  await mockApp(page, state);
  const other = await context.newPage();
  await mockApp(other, state);
  // Installed BEFORE navigation so the bell's `setInterval` is created against the fake clock.
  // This is what lets the assertion below drive the REAL polling boundary: a `reload()` would
  // only prove startup fetching, and would stay green with the interval deleted.
  await other.clock.install();

  await openConsole(page);
  await openConsole(other);

  const otherBell = other.getByRole("button", { name: /^Notifications/ });
  await expect(otherBell).toHaveAttribute("aria-label", "Notifications, 1 unread");

  await page.getByRole("button", { name: /^approve$/i }).click();
  await expect(
    page.getByRole("button", { name: /^Notifications/ }),
  ).toHaveAttribute("aria-label", "Notifications");

  // The event is same-tab BY DESIGN — deliberately not a BroadcastChannel. If this ever starts
  // updating instantly, the contract documented in `lib/actionEvents.ts` has silently changed
  // and the 60s poll is no longer the other tab's convergence path.
  await expect(otherBell).toHaveAttribute("aria-label", "Notifications, 1 unread");

  // …and it converges on its own POLL — the already-open page, never reloaded. Advancing the
  // fake clock past POLL_MS fires the real `setInterval`, so deleting that interval fails this
  // assertion; a `reload()` here would pass without it and prove only startup fetching.
  await other.clock.fastForward(61_000);
  await expect(otherBell).toHaveAttribute("aria-label", "Notifications");
});
