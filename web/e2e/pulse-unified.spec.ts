import { expect, test, type Page } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";

import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
} from "./mission-console";
import { setupBench } from "./terminal/harness";

// #754 — the orchestrator queue merged into the session cards, plus project/agent filters.
//
// Measured against the live stores before this change: 16 sessions had a live action and all 16
// already appeared under "Needs you", with 0 exclusive to the queue. So the queue rendered one
// session twice, in two visual languages, with two different affordances.
//
// #948 P3 moved the surfaces again, and this file follows them rather than the old markup:
//   - a decision for a session NO mission holds renders in that session's pane
//     (`session-decisions`), since the "Sessions without a mission" view is gone;
//   - a decision for a session a mission HOLDS renders in that mission's thread, and settling it
//     still goes through the route's local settlement + overview re-read (`Pulse.tsx`), which is
//     what the #762 refresh-failure cases below were written against.
//
// REMOVED with the untracked view (#948 P3), each for the same reason — the UI no longer exists:
//   - "a card with a live action sorts above one without": there is no session list under
//     /mission to order any more.
//   - "project and agent filters narrow the whole list and compose": the untracked project/agent
//     selects are gone; the sessions sidebar's filters are pinned in
//     `src/components/sidebar/Filters.test.tsx`.

const NOW = Math.floor(Date.now() / 1000);

const UUID = "aaaaaaaa-0000-4000-8000-000000000754";
const OTHER = "bbbbbbbb-0000-4000-8000-000000000754";
/** A server-shaped mission id (`msn_` + 32 hex), so the `?m=` deep link is honoured. */
const MID = `msn_${"754a".repeat(8)}`;

const ORCH_CONFIG = {
  enabled: true,
  autonomy: "suggest",
  allowed_verbs: ["continue"],
  auto_verbs_ceiling: ["continue"],
  confidence_min: 0.75,
  interval_minutes: 10,
  max_actions_per_pass: 4,
  proposal_ttl_minutes: 30,
  nudge_template: "Please continue.",
  notify: "escalations",
  configured: true,
  default_nudge_template: "Please continue.",
};

const ACTION = {
  id: "act-1",
  state: "proposed",
  ts: NOW,
  expires_at: NOW + 1800,
  tier: "suggest",
  session_id: `claude:${UUID}`,
  engine: "claude",
  title: "Switch the default model",
  project: "infra",
  project_id: "p1",
  verb: "continue",
  confidence: 0.86,
  rationale: "finished the edit and stopped without confirming",
  evidence: "none",
};

/** A second decision, on a second session, that must SURVIVE the first one settling. */
const OTHER_ACTION = {
  ...ACTION,
  id: "act-2",
  session_id: `codex:${OTHER}`,
  engine: "codex",
  title: "Relay cap",
  project: "battlelab",
  project_id: "p2",
  rationale: "stopped before running the relay tests",
};

function card(over: Record<string, unknown> = {}) {
  return {
    id: `claude:${UUID}`,
    engine: "claude",
    title: "Switch the default model",
    cwd: "/home/u/infra",
    project: { kind: "project", id: "p1", name: "infra", color: "#ffb000" },
    state: "needs_you",
    live: false,
    last_activity: NOW - 600,
    last_mtime: NOW - 600,
    intervention_required: false,
    ai_summary: "Editing opencode.json",
    synthesis: "",
    ...over,
  };
}

function overview(cards: unknown[]) {
  return {
    cache_version: 2,
    generated_at: NOW,
    window_days: 3,
    scan_depth: "slow",
    input_fingerprint: null,
    synthesis_skipped: false,
    banner: null,
    cards,
  };
}

function orchestrator(pending: unknown[], feed: unknown[] = []) {
  return {
    config: ORCH_CONFIG,
    pending,
    feed,
    expired_now: 0,
    running: [],
    last: {},
  };
}

test.beforeEach(async ({ page }) => {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        pulse: {
          auto_enabled: false,
          interval_minutes: 30,
          window_days: 3,
          scan_depth: "slow",
          configured: true,
        },
        orchestrator: ORCH_CONFIG,
      },
    }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route(/\/api\/projects($|\?)/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
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
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    r.fulfill({ json: orchestrator([ACTION]) }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: overview([
        card({ pending_action: ACTION }),
        card({
          id: `codex:${OTHER}`,
          engine: "codex",
          title: "Relay cap",
          project: {
            kind: "project",
            id: "p2",
            name: "battlelab",
            color: "#ffb000",
          },
          state: "needs_you",
        }),
      ]),
    }),
  );
});

/** The pane of the session no mission holds — where its decision renders since #948 P3. */
async function openPane(page: Page) {
  await setupBench(page, {
    sessions: [{ engine: "claude", uuid: UUID, title: ACTION.title }],
  });
  await page.goto(`/s/claude/${UUID}`);
  return page.getByTestId("session-decisions");
}

/** A mission holding `keys`, opened by its deep link — its thread is where their decisions render. */
async function openMissionHolding(page: Page, keys: string[]) {
  await mockMissions(page, {
    missions: missionList([
      missionRow({ id: MID, title: "Unify the queue", session_keys: keys }),
    ]),
    mission: {
      ...MISSION,
      id: MID,
      title: "Unify the queue",
      sessions: keys.map((k) => ({ session_key: k, removed_at: null })),
      events: [],
      events_next_seq: null,
    },
  });
  await page.goto(`/mission?m=${MID}`);
  await expect(page.getByTestId("console-title")).toHaveText("Unify the queue");
}

test("a pending decision appears ONCE, in its session's pane, with its controls inside it", async ({
  page,
}) => {
  const strip = await openPane(page);
  await expect(strip).toHaveCount(1);

  // The decision's own words appear exactly once on the page — the queue used to render the
  // action a second time in its own list.
  await expect(page.getByText(ACTION.rationale)).toHaveCount(1);
  // …and the strip does not restate the session's identity: the pane already names it, which is
  // why `ActionRow` renders embedded here, as it did inside the card.
  await expect(strip).not.toContainText(ACTION.title);

  // The controls are INSIDE that strip, not in a separate block.
  const approve = page.getByRole("button", { name: /^approve$/i });
  await expect(approve).toHaveCount(1);
  const stripBox = (await strip.boundingBox())!;
  const btnBox = (await approve.boundingBox())!;
  expect(btnBox.y).toBeGreaterThan(stripBox.y);
  expect(btnBox.y).toBeLessThan(stripBox.y + stripBox.height);
});

test("the manual pass lives in Settings and reports what the pass actually said (#929)", async ({
  page,
}) => {
  // #754's defect was a panel on /mission claiming "N actions need you" while no card carried
  // one. #929 removes that panel, so the claim cannot desync — but "Run now" was the operator's
  // only way to force a pass, and the degraded badge still points at it, so it moved to
  // Settings rather than going away. This asserts the control exists there and surfaces the
  // pass's own words; a fixed "done" string would report a result the server never gave.
  let scanned = false;
  await page.unroute(/\/api\/pulse$/);
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: overview([
        scanned ? card({ pending_action: ACTION }) : card({ state: "idle" }),
      ]),
    }),
  );
  await page.route(/\/api\/pulse\/orchestrate$/, async (r) => {
    scanned = true;
    await r.fulfill({
      json: {
        assessment: "one action",
        pending: [ACTION],
        feed: [],
        config: ORCH_CONFIG,
      },
    });
  });

  await mockMissions(page);
  await page.goto(settingsPath("ai-mission-control"));
  const run = page.getByRole("button", { name: /run now/i });
  await expect(run).toBeVisible();
  await run.click();

  // The pass's own assessment, not a canned string.
  await expect(
    page.getByText("one action", { exact: true }).first(),
  ).toBeVisible();
  expect(scanned).toBe(true);

  // And the route no longer carries a second copy of the control (#929).
  await page.goto("/mission");
  await expect(page.getByRole("button", { name: /run now/i })).toHaveCount(0);
});

test("resolving a mission's decision still re-reads the orchestrator's own state", async ({
  page,
}) => {
  // The health badge owns its own read of the orchestrator. Without telling it, approving a
  // decision left "1 action needs you" sitting above a row that no longer had one (#754 review).
  // #929 removed that headline, but not the wiring underneath it — the badge reads the same
  // endpoint, so a settled action must still refresh it. The fetch count is deliberately what is
  // asserted; with the headline gone it is the ONLY honest witness.
  let settled = false;
  const key = `claude:${UUID}`;
  await page.unroute(/\/api\/pulse$/);
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: overview([
        settled
          ? card({ mission_id: MID })
          : card({ mission_id: MID, pending_action: ACTION }),
      ]),
    }),
  );
  await page.unroute(/\/api\/pulse\/orchestrator$/);
  await page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    r.fulfill({
      json: settled
        ? orchestrator([], [{ ...ACTION, state: "delivered" }])
        : orchestrator([ACTION]),
    }),
  );
  await page.route(/\/api\/pulse\/actions\/.*\/approve$/, async (r) => {
    settled = true;
    await r.fulfill({ json: { ...ACTION, state: "delivered" } });
  });

  // Count the endpoint's OWN fetches. Asserting on rendered text instead was useless: the text
  // also disappears if the component happens to remount, so the assertion passed with the wiring
  // removed. This measures the thing the fix actually does.
  let orchFetches = 0;
  page.on("request", (req) => {
    if (/\/api\/pulse\/orchestrator$/.test(new URL(req.url()).pathname))
      orchFetches += 1;
  });

  await openMissionHolding(page, [key]);
  const approve = page.getByRole("button", { name: /^approve$/i });
  await expect(approve).toBeVisible();
  const before = orchFetches;

  await approve.click();
  await expect(approve).toHaveCount(0);

  // The orchestrator's own state is re-read, so nothing downstream of it can disagree with
  // the decisions on screen.
  await expect.poll(() => orchFetches).toBeGreaterThan(before);
});

test("a settled action loses its controls even when the background refresh fails", async ({
  page,
}) => {
  // Approve succeeds; the refetch that was supposed to remove the row does not. Its catch is
  // silent by design, so the stale `pending_action` survived while `ActionRow` cleared `busy`
  // in its `finally` — leaving Approve/Reject enabled for an action the server had already
  // decided, until the operator reloaded the page (#762 review). The response already says
  // what happened, so the settlement is applied locally and the refetch only reconciles.
  //
  // Read in a MISSION'S THREAD since #948 P3: that is where the route's local settlement still
  // decides what is drawn. (The card's LED band assertion that followed went with the untracked
  // view — no surface under /mission draws a card's band any more.)
  let approved = false;
  await page.unroute(/\/api\/pulse$/);
  await page.route(/\/api\/pulse$/, async (r) => {
    if (approved) return r.fulfill({ status: 500, json: { detail: "boom" } });
    await r.fulfill({
      json: overview([
        // Exactly what `_attach_pending` emits: re-banded to `needs_you`, with the band it had
        // before the overlay preserved so the client can put it back.
        card({
          mission_id: MID,
          pending_action: ACTION,
          state: "needs_you",
          state_without_action: "idle",
        }),
        card({
          id: `codex:${OTHER}`,
          engine: "codex",
          title: "Relay cap",
          mission_id: MID,
          pending_action: OTHER_ACTION,
        }),
      ]),
    });
  });
  await page.route(/\/api\/pulse\/actions\/.*\/approve$/, async (r) => {
    approved = true;
    await r.fulfill({ json: { ...ACTION, state: "delivered" } });
  });

  await openMissionHolding(page, [`claude:${UUID}`, `codex:${OTHER}`]);
  const row = page.locator("li", { hasText: ACTION.rationale });
  const approve = row.getByRole("button", { name: /^approve$/i });
  await expect(approve).toBeVisible();
  await expect(page.getByText(OTHER_ACTION.rationale)).toBeVisible();
  await approve.click();

  // Two assertions this test needs to be worth anything:
  //
  // The OTHER decision must still be there. Without that, a blanked overview (a 500 body applied
  // as if it were an overview) removes every row too and the test passes for the wrong reason.
  await expect(page.getByText(OTHER_ACTION.rationale)).toBeVisible();
  // And the check must be on something STABLE. `Approve` relabels itself to `Sending…` while
  // the request is in flight, so asserting the button is gone passes during that window —
  // against the unfixed code as well. The action's rationale only leaves the DOM when the row
  // itself does.
  await expect(page.getByText(ACTION.rationale)).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^approve$/i })).toHaveCount(1);
});

test("a settled action loses its controls in the SESSION PANE too, even when the re-read fails", async ({
  page,
}) => {
  // The same #762 guarantee on the surface an unheld session's decision moved to (#948 P3). The
  // pane refreshes its decisions from `GET /api/pulse/orchestrator`; when that re-read fails after
  // a successful approve, the settled action must not sit there offering Approve again.
  let approved = false;
  const SAME_SESSION = {
    ...ACTION,
    id: "act-3",
    rationale: "a second question on the same session",
    evidence: "none",
    verb: "escalate",
    state: "escalated",
  };
  await page.unroute(/\/api\/pulse\/orchestrator$/);
  await page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    approved
      ? r.fulfill({ status: 500, json: { detail: "boom" } })
      : r.fulfill({ json: orchestrator([ACTION, SAME_SESSION]) }),
  );
  await page.route(/\/api\/pulse\/actions\/.*\/approve$/, async (r) => {
    approved = true;
    await r.fulfill({ json: { ...ACTION, state: "delivered" } });
  });

  const strip = await openPane(page);
  await expect(strip.getByText(SAME_SESSION.rationale)).toBeVisible();
  await strip.getByRole("button", { name: /^approve$/i }).click();

  // The untouched decision proves the strip was not simply blanked…
  await expect(strip.getByText(SAME_SESSION.rationale)).toBeVisible();
  // …and the settled one is gone, anchored on its rationale rather than the relabelling button.
  await expect(page.getByText(ACTION.rationale)).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^approve$/i })).toHaveCount(0);
});

test("a card that existed only for its action goes away with it", async ({
  page,
}) => {
  // `_attach_pending` synthesizes a card when a live action has no cached card. Settle that
  // action and there is nothing left to show: no summary, no controls, no real session row
  // (#762 review). Read in a mission's thread since #948 P3 — the route's settlement branch for a
  // synthesized card is unchanged, and a real decision beside it must be untouched.
  let approved = false;
  const PHANTOM = {
    ...OTHER_ACTION,
    id: "act-phantom",
    rationale: "a proposal for a session with no cached card",
  };
  await page.unroute(/\/api\/pulse$/);
  await page.route(/\/api\/pulse$/, async (r) => {
    if (approved) return r.fulfill({ status: 500, json: { detail: "boom" } });
    await r.fulfill({
      json: overview([
        card({ mission_id: MID, pending_action: ACTION }),
        {
          id: `codex:${OTHER}`,
          engine: "codex",
          title: "Phantom candidate",
          cwd: "",
          project: { kind: "project", id: "p3", name: "relay" },
          state: "needs_you",
          synthesized_for_action: true,
          mission_id: MID,
          live: false,
          last_activity: NOW - 60,
          intervention_required: false,
          intervention_reason: "",
          ai_summary: "",
          synthesis: "",
          pending_action: PHANTOM,
        },
      ]),
    });
  });
  await page.route(/\/api\/pulse\/actions\/.*\/approve$/, async (r) => {
    approved = true;
    await r.fulfill({ json: { ...PHANTOM, state: "delivered" } });
  });

  await openMissionHolding(page, [`claude:${UUID}`, `codex:${OTHER}`]);
  const phantomRow = page.locator("li", { hasText: PHANTOM.rationale });
  await expect(phantomRow).toBeVisible();

  await phantomRow.getByRole("button", { name: /^approve$/i }).click();

  await expect(page.getByText(PHANTOM.rationale)).toHaveCount(0);
  // The real session's decision is untouched.
  await expect(page.getByText(ACTION.rationale)).toBeVisible();
});
