/** The operator's controls, in a real browser (#889).
 *
 * jsdom can prove that a handler posts the right body. It cannot prove the thing this PR is
 * actually about: that after a REFUSED transition the console shows the mission the server has,
 * not the one the operator asked for. That is a render decided by a fetch that happens after a
 * rejected promise, in a component tree with a poll running underneath it, and it is exactly the
 * shape that passes in an emulator and ships broken.
 *
 * So the assertions here are on the REQUESTS the app makes and on what is painted afterwards —
 * never on a DOM proxy for either.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
  openMissionRail,
} from "./mission-console";

const T = 1_700_000_000;

const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
  pulse: { configured: true },
};

const OVERVIEW = {
  cache_version: 1,
  generated_at: T - 60,
  window_days: 3,
  scan_depth: "medium",
  input_fingerprint: "fp",
  synthesis_skipped: false,
  banner: null,
  cards: [],
};

async function stub(page: Page) {
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
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
    r.fulfill({
      json: { notifications: [], unread: 0, uncertain: 0, settled: [] },
    }),
  );
  await page.route(/\/api\/pulse$/, (r) => r.fulfill({ json: OVERVIEW }));
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({
      json: {
        projects: [
          {
            id: "p1",
            name: "agent-sessions",
            color: "#ffb000",
            folders: ["/repo"],
            default_folder: "/repo",
            archived: false,
            created_at: T,
            session_count: 0,
          },
        ],
      },
    }),
  );
}

/** A mission holding one session, in `running`. */
/** Select a mission in the rail, on WHATEVER viewport the project supplies.
 *
 *  The rail is a column at >=1100px and a DRAWER below it, so a test that forced 1440x900 ran the
 *  desktop layout under both project names — 15 of 16 cases were desktop duplicated, which is not
 *  the mobile acceptance #889 asks for (#896 review 3, finding 5). Opening the drawer when there
 *  is one is the only difference between the two, so it lives here rather than in every test. */
async function ready(page: Page) {
  // `isVisible()` answers about NOW, so asking before the console has painted reports "no drawer"
  // and "no stop strip" on a phone and every mobile navigation below silently does nothing.
  await expect(page.getByTestId("mission-console")).toBeVisible();
}

/** The rail rows the operator can actually see.
 *
 *  On a phone the rail is rendered TWICE — inline (hidden by CSS) and inside the drawer — so a
 *  bare `getByTestId("rail-mission")` resolves to two elements and both clicks and counts are
 *  wrong. `:visible` picks the one on screen, whichever layout that is. */
function railRows(page: Page) {
  return page.locator('[data-testid="rail-mission"]:visible');
}

/** Every mission title the rail shows more than once (#896 review 15, finding 2). Empty is the
 *  contract: within one consistent snapshot the server cannot return the same mission twice, so a
 *  repeat is proof the pages were stitched from two. */
async function railDupes(page: Page) {
  const titles = (await railRows(page).allInnerTexts()).map(
    (s) => (s.match(/M\d+/) || [""])[0],
  );
  return [...new Set(titles.filter((x, i) => titles.indexOf(x) !== i))];
}

/** Close the rail drawer if one is open. On a phone it is a modal over the composer, so leaving
 *  it open makes every subsequent click land on the scrim. */
async function closeRail(page: Page) {
  // The SHELL's drawer, which is the only dialog on this page (#940). `rail-drawer` was
  // `MissionDrawer`'s panel and that component is deleted, so this used to match nothing and
  // leave the drawer open over every subsequent click.
  if (await page.getByRole("dialog").count()) {
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
  }
}

/** Flip the Active/Archived scope, opening the drawer first where the rail is one. */
async function toggleScope(page: Page) {
  await ready(page);
  await openMissionRail(page);
  await page.locator('[data-testid="rail-scope"]:visible').click();
  await closeRail(page);
}

/** How many missions the rail is showing, on either layout. Opens the drawer to look and closes
 *  it again, so a count never leaves a modal sitting over the next interaction. */
async function railCount(page: Page): Promise<number> {
  await ready(page);
  // Opened through the SHELL (#940). The console's own `☰` is gone, so `isVisible()` on it was
  // always false and the drawer never opened — every mobile count below read an off-canvas rail,
  // which Playwright calls "visible" because it has a box.
  await openMissionRail(page);
  const n = await railRows(page).count();
  await closeRail(page);
  return n;
}

/** The mission named is the one the console is on (#935).
 *
 *  These sites used to read `expect(mission-console).toContainText("Bravo")`, which passed
 *  because the RAIL was a descendant of the console and listed every mission's name — so the
 *  assertion held whether or not the click had selected anything. #935 moves the rail into the
 *  app shell's sidebar and the weakness became visible as four failures.
 *
 *  Asserting `aria-current` on the row says what the test means, and says it about the selection
 *  rather than about a list that happens to mention the word. */
async function expectMissionSelected(page: Page, name: string | RegExp) {
  // NOT `railRows`, which filters on `:visible`. On a phone the rail lives in a drawer that
  // `selectMission` closes behind itself, so the selected row is present and correct but not
  // visible — and a visibility-filtered locator would report "element(s) not found" for a
  // selection that is entirely fine.
  await expect(
    page
      .locator('[data-testid="rail-mission"]')
      .filter({ hasText: name })
      .first(),
  ).toHaveAttribute("aria-current", "true");
}

async function selectMission(page: Page, name: string | RegExp) {
  await ready(page);
  await openMissionRail(page);
  await railRows(page).filter({ hasText: name }).click();
  await closeRail(page);
}

/** Reach the OBJECTIVES content, on whatever viewport the project supplies.
 *
 *  At >=1400px the detail column is always on screen; below it the same content is behind the
 *  OBJECTIVES stop. Every objective, follow-through and context control lives there, so a test
 *  that only clicked them at 1440px was not testing the phone at all — which is what the forced
 *  viewport was hiding. A no-op where the stop strip is not rendered. */
async function goToObjectives(page: Page) {
  await ready(page);
  const stop = page.getByTestId("stop-objectives");
  if (await stop.isVisible().catch(() => false)) await stop.click();
}

/** …and the UNTRACKED view, same reason. */
async function selectUntracked(page: Page) {
  await ready(page);
  await openMissionRail(page);
  await page.locator('[data-testid="rail-untracked-view"]:visible').click();
}

/** How many rows the rail shows, on whatever layout the project supplies.
 *
 *  Through the SHELL's control since #940; the console's own `☰` is gone.
 *
 *  THE OPENING IS NO LONGER BEST-EFFORT (#940 review 4). This swallowed a failed open with
 *  `.catch(() => undefined)`, on the reasoning that the opener re-renders as the rail updates and
 *  the COUNT is what the caller polls — but a suppressed open means the count is taken from a
 *  PARKED panel, and `expect.poll` then retries a number that can never move while reporting the
 *  timeout as if the rail were wrong. The helper itself stopped swallowing its readiness waits;
 *  leaving the suppression here would keep the same silence one level up. */
async function railRowCount(page: Page) {
  await openMissionRail(page);
  const n = await railRows(page).count();
  // CLOSED AGAIN, always. Leaving it open makes the NEXT helper's opener sit behind the scrim,
  // where it resolves but never becomes stable — a 30s hang that looks like a product bug.
  await closeRail(page);
  return n;
}

const HELD_ROW = missionRow({ session_keys: ["claude:aaa"] });
const HELD = {
  ...MISSION,
  sessions: [{ session_key: "claude:aaa", removed_at: null }],
};

test("NEW MISSION posts the instruction and the project ENTITY id, and never a cwd", async ({
  page,
}) => {
  await stub(page);
  await mockMissions(page, { missions: missionList([]) });

  const posts: { url: string; body: unknown }[] = [];
  // Registered AFTER mockMissions so it wins for this exact URL (most-recent-first).
  await page.route("**/api/missions", async (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    posts.push({ url: r.request().url(), body: r.request().postDataJSON() });
    return r.fulfill({
      status: 201,
      json: { ...MISSION, id: "msn_new", state: "draft", sessions: [] },
    });
  });

  await page.goto("/pulse");
  await expect(page.getByTestId("mission-console")).toBeVisible();

  await page.getByTestId("composer-mode-new").click();
  await page
    .getByTestId("new-mission-instruction")
    .fill("implement the probe runner");
  await page.getByTestId("new-mission-project").selectOption("p1");
  await page.getByTestId("new-mission-start").click();

  await expect.poll(() => posts.length).toBe(1);
  const body = posts[0].body as Record<string, unknown>;
  expect(body.instruction).toBe("implement the probe runner");
  expect(body.project_id).toBe("p1");
  // The route REFUSES a client-sent cwd (422). Asserting on the wire, because this is the
  // property that keeps a mission's working directory out of the client's hands.
  expect(Object.keys(body)).not.toContain("cwd");
});

test("a REFUSED transition paints the server's state, not the one that was asked for", async ({
  page,
}) => {
  await stub(page);

  // The mission is `running` when the page loads. The transition is then refused — somebody else
  // closed it as `failed` — and the re-read returns that. A console that applied the request
  // optimistically would paint `done` here, which is the whole bug.
  let refused = false;
  await mockMissions(page, {
    missions: () =>
      missionList([
        refused
          ? missionRow({ session_keys: ["claude:aaa"], state: "failed" })
          : HELD_ROW,
      ]),
    mission: undefined,
  });
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    return r.fulfill({
      json: {
        ...HELD,
        state: refused ? "failed" : "running",
        outcome: refused ? "failed" : null,
        events: [],
        events_next_seq: null,
      },
    });
  });
  await page.route("**/api/missions/*/state", (r) => {
    refused = true;
    return r.fulfill({
      status: 409,
      json: { detail: "mission msn_1 is no longer running" },
    });
  });

  await page.goto("/pulse");
  await expect(page.getByTestId("mission-state")).toHaveText("running");

  // MARK DONE confirms — closing releases every session the mission holds, so the
  // first tap only asks (#896 review 6, finding 2).
  await page.getByTestId("mission-done").click();
  await page.getByTestId("mission-done").click();

  // The state the SERVER has, painted after the re-read. `done` was never true.
  await expect(page.getByTestId("mission-state")).toHaveText("failed");
  await expect(page.getByTestId("mission-state")).not.toHaveText("done");
});

test("BEGIN is refused without a session, and says what unblocks it", async ({
  page,
}) => {
  await stub(page);
  const planned = { ...MISSION, state: "planned", sessions: [] };
  await mockMissions(page, {
    missions: missionList([missionRow({ state: "planned", session_keys: [] })]),
    mission: { ...planned, events: [], events_next_seq: null },
  });
  await page.goto("/pulse");

  const begin = page.getByTestId("mission-begin");
  await expect(begin).toBeVisible();
  await expect(begin).toBeDisabled();
  // The reason is ON the control. A disabled button with no explanation is a dead end.
  await expect(begin).toHaveAttribute("title", /adopt a session/i);
});

test("an objective edit posts ONE batch of ops", async ({ page }) => {
  await stub(page);
  const patches: unknown[] = [];
  await mockMissions(page, {
    missions: missionList([HELD_ROW]),
    mission: { ...HELD, events: [], events_next_seq: null },
    objectives: {
      objectives: [
        {
          mission_id: HELD.id,
          key: "pr_open",
          ord: 0,
          title: "A PR is open",
          probe: "forge_pr",
          probe_args: null,
          gate: true,
          state: "pending",
          met_at: null,
          observed: null,
          source: "playbook",
        },
      ],
    },
  });
  await page.route("**/api/missions/*/objectives", async (r) => {
    if (r.request().method() !== "PATCH") return r.fallback();
    patches.push(r.request().postDataJSON());
    return r.fulfill({ json: { objectives: [] } });
  });

  await page.goto("/pulse");
  await goToObjectives(page);
  await expect(page.getByTestId("objective").first()).toBeVisible();

  await page.getByTestId("objective-waive").first().click();
  await expect.poll(() => patches.length).toBe(1);
  // The route applies ops in ONE transaction; a request per op would let a multi-op edit
  // half-apply. And `state` is not among the keys — an edit is never a claim it holds.
  expect(patches[0]).toEqual({ ops: [{ op: "waive", key: "pr_open" }] });
});

test("STAND DOWN posts the episode the board was RENDERED at", async ({
  page,
}) => {
  await stub(page);
  const posts: unknown[] = [];
  await mockMissions(page, {
    missions: missionList([HELD_ROW]),
    mission: {
      ...HELD,
      events: [],
      events_next_seq: null,
      supervisor: {
        objectives: [
          {
            key: "checks",
            title: "Checks are green",
            gate: true,
            state: "pending",
            met: false,
            episode: 4,
            stood_down: false,
            spent: 1,
            remaining: 2,
            may_nudge: true,
            unreadable: false,
            indeterminate: false,
            live: 0,
            terminal: false,
            why_not: "",
          },
        ],
        likely_done: false,
        unmet_gates: 1,
        checked_at: T,
      },
    },
  });
  await page.route("**/api/missions/*/objectives/*/stand-down", (r) => {
    posts.push({ url: r.request().url(), body: r.request().postDataJSON() });
    return r.fulfill({ json: { episode: 4, stood_down: true } });
  });

  await page.goto("/pulse");
  await goToObjectives(page);
  const btn = page.getByTestId("objective-stand-down").first();
  await expect(btn).toBeVisible();
  await btn.click();

  await expect.poll(() => posts.length).toBe(1);
  const p = posts[0] as { url: string; body: { episode: number } };
  expect(p.url).toContain("/objectives/checks/stand-down");
  // Four, because that is what the row said. Sending the CURRENT episode would silence a report
  // nobody has seen — the server answers 409 for exactly that case and the client must let it.
  expect(p.body.episode).toBe(4);
});

test("archiving a live mission asks first and says it stops the agents", async ({
  page,
}) => {
  await stub(page);
  const posts: unknown[] = [];
  await mockMissions(page, {
    missions: missionList([HELD_ROW]),
    mission: { ...HELD, events: [], events_next_seq: null },
  });
  await page.route("**/api/missions/*/archive", (r) => {
    posts.push(r.request().postDataJSON());
    return r.fulfill({ json: { mission: HELD } });
  });

  await page.goto("/pulse");
  await page.getByTestId("mission-archive").click();

  // Nothing has happened yet, and the copy says what it will do — masters stopped, transcripts
  // kept, reversible.
  expect(posts).toHaveLength(0);
  const confirm = page.getByTestId("mission-confirm-archive");
  await expect(confirm).toContainText(/abandon it first/i);
  await expect(confirm).toContainText(/transcript is kept/i);

  await page.getByTestId("mission-archive").click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toEqual({ abandon: true });
});

test("a refusal for the mission you LEFT is discarded, not shown over the one you moved to", async ({
  page,
}) => {
  await stub(page);

  const A = missionRow({
    id: "msn_a",
    title: "Alpha",
    session_keys: ["claude:aaa"],
  });
  const B = missionRow({ id: "msn_b", title: "Bravo", session_keys: [] });
  await mockMissions(page, { missions: missionList([A, B]) });
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: id === "msn_a" ? "Alpha" : "Bravo",
        state: "running",
        sessions:
          id === "msn_a"
            ? [{ session_key: "claude:aaa", removed_at: null }]
            : [],
        events: [],
        events_next_seq: null,
      },
    });
  });

  // HELD OPEN. The transition for Alpha is still in flight when the operator moves to Bravo; it
  // is released afterwards, so the refusal resolves against a console showing a different
  // mission — the exact ordering that files A's error over B.
  let release: (() => void) | undefined;
  const held = new Promise<void>((r) => (release = r));
  await page.route("**/api/missions/*/state", async (r) => {
    await held;
    return r.fulfill({
      status: 409,
      json: { detail: "mission msn_a is no longer running" },
    });
  });

  await page.goto("/pulse");
  await selectMission(page, "Alpha");
  await expect(page.getByTestId("mission-state")).toHaveText("running");
  // MARK DONE confirms — closing releases every session the mission holds, so the
  // first tap only asks (#896 review 6, finding 2).
  await page.getByTestId("mission-done").click();
  await page.getByTestId("mission-done").click();

  // Move on while it is still in flight.
  await selectMission(page, "Bravo");
  await expectMissionSelected(page, "Bravo");

  release?.();

  // Alpha's refusal must never appear here. Waiting a beat so a note that WOULD land has landed —
  // asserting immediately would pass against the broken implementation too.
  await page.waitForTimeout(750);
  await expect(page.getByTestId("console-note")).toHaveCount(0);
});

// ==============================================================================================
// #896 review round 1 — findings 3, 4 and 6, each as the browser probe the review used.
// ==============================================================================================

test("creating from the ARCHIVED rail never issues an archived list request afterwards", async ({
  page,
}) => {
  await stub(page);
  const scopes: string[] = [];
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (r.request().method() === "POST") {
      return r.fulfill({
        status: 201,
        json: { ...MISSION, id: "msn_new", state: "draft", sessions: [] },
      });
    }
    // Only the LIST — `/api/missions/{id}` is routed more specifically below.
    if (u.pathname === "/api/missions") {
      scopes.push(
        u.searchParams.get("archived") === "1" ? "archived" : "active",
      );
      return r.fulfill({ json: missionList([]) });
    }
    return r.fallback();
  });
  await page.route("**/api/missions/*", (r) =>
    r.fulfill({ json: { ...MISSION, events: [], events_next_seq: null } }),
  );

  await page.goto("/pulse");
  await expect(page.getByTestId("mission-console")).toBeVisible();

  await toggleScope(page); // → Archived
  await expect.poll(() => scopes.at(-1)).toBe("archived");
  const before = scopes.length;

  await page.getByTestId("composer-mode-new").click();
  await page.getByTestId("new-mission-instruction").fill("start it here");
  // A project is REQUIRED — a mission without one has no cwd and no way to acquire one from
  // this console, so START is withheld until one is chosen (#896 review 9, finding 2).
  await page.getByTestId("new-mission-project").selectOption("p1");
  await page.getByTestId("new-mission-start").click();

  await expect.poll(() => scopes.length).toBeGreaterThan(before);
  await page.waitForTimeout(500);
  // Every request issued AFTER the create is for the active scope. `reload()` is closed over the
  // old `archived`, and `setScope` has already bumped the generation — so an archived response
  // would share the new generation and overwrite the active rail.
  expect(scopes.slice(before)).not.toContain("archived");
});

test("archiving the mission you SELECTED does not leave it on screen", async ({
  page,
}) => {
  await stub(page);
  // TWO missions, and the operator SELECTS the second explicitly. That is load-bearing: `shown`
  // is `selected ?? autoSelected`, and only the explicit half survives a list that no longer
  // contains it. A version of this test that let the mission be auto-selected passed against the
  // unfixed code, because `autoSelected` re-derives from the current list and quietly did the
  // right thing — the bug lives entirely in the explicit selection.
  const KEEP = missionRow({ id: "msn_keep", title: "Keep" });
  const GO = missionRow({ id: "msn_go", title: "Going", state: "done" });
  let archived = false;
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions" || r.request().method() !== "GET")
      return r.fallback();
    const wantArchived = u.searchParams.get("archived") === "1";
    if (wantArchived)
      return r.fulfill({ json: missionList(archived ? [GO] : []) });
    return r.fulfill({ json: missionList(archived ? [KEEP] : [KEEP, GO]) });
  });
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: id === "msn_go" ? "Going" : "Keep",
        state: id === "msn_go" ? "done" : "running",
        outcome: id === "msn_go" ? "done" : null,
        archived_at: id === "msn_go" && archived ? 1 : null,
        sessions: [],
        events: [],
        events_next_seq: null,
      },
    });
  });
  await page.route("**/api/missions/*/archive", (r) => {
    archived = true;
    return r.fulfill({ json: { mission: { ...MISSION, id: "msn_go" } } });
  });

  await page.goto("/pulse");
  await selectMission(page, "Going");
  await expectMissionSelected(page, "Going");

  await page.getByTestId("mission-archive").click();
  await page.getByTestId("mission-archive").click();

  // The rail is Active and "Going" is not in it any more, so the body must not still be showing
  // it: a rail and a body describing different sets is the failure `setScope` exists to prevent
  // for the scope toggle, and archiving reaches it by another route.
  await expect.poll(() => railCount(page)).toBe(1);
  await expect(page.getByTestId("mission-console")).not.toContainText("Going");
});

test("a refused APPROVAL for the mission you left is discarded too", async ({
  page,
}) => {
  await stub(page);
  const A = missionRow({
    id: "msn_a",
    title: "Alpha",
    session_keys: ["claude:aaa"],
  });
  const B = missionRow({ id: "msn_b", title: "Bravo", session_keys: [] });
  // The card carries the pending action, which is how a decision reaches the mission thread.
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        ...OVERVIEW,
        cards: [
          {
            id: "claude:aaa",
            engine: "claude",
            title: "Alpha's session",
            cwd: "/repo",
            last_activity: T - 60,
            live: true,
            state: "needs_you",
            intervention_required: false,
            intervention_reason: "",
            pending_action: {
              id: "act_1",
              session_id: "claude:aaa",
              verb: "continue",
              state: "proposed",
              text: "carry on",
              confidence: 0.9,
              announced: false,
            },
          },
        ],
      },
    }),
  );
  await mockMissions(page, { missions: missionList([A, B]) });
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        sessions:
          id === "msn_a"
            ? [{ session_key: "claude:aaa", removed_at: null }]
            : [],
        events: [],
        events_next_seq: null,
      },
    });
  });

  let release: (() => void) | undefined;
  const held = new Promise<void>((r) => (release = r));
  await page.route("**/api/pulse/actions/*/approve", async (r) => {
    await held;
    // A SETTLED RECORD, not a bare `{detail}`. `ActionRow` only raises the explanation to the
    // panel when the 409 body is shape-checked as an action (`rec.id && rec.state`); a
    // detail-only 409 keeps its note on the row, which never reaches the console and would make
    // this test pass against the unfenced code. That is exactly what happened on the first
    // attempt — the mutation ran green and the test proved nothing.
    return r.fulfill({
      status: 409,
      json: {
        detail: "that action is no longer yours to approve",
        id: "act_1",
        session_id: "claude:aaa",
        verb: "continue",
        state: "stale",
        text: "carry on",
        confidence: 0.9,
      },
    });
  });

  await page.goto("/pulse");
  await selectMission(page, "Alpha");
  const approve = page.getByRole("button", { name: /approve/i }).first();
  await expect(approve).toBeVisible();
  await approve.click();

  await selectMission(page, "Bravo");
  await expectMissionSelected(page, "Bravo");
  release?.();

  await page.waitForTimeout(750);
  // Same rule as the lifecycle path, one consumer further along: `ActionRow` calls the console's
  // note callback from its own 409 branch.
  await expect(page.getByTestId("console-note")).toHaveCount(0);
});

test("RELEASE puts the session back in UNTRACKED, not just posts a detach", async ({
  page,
}) => {
  // The first version of this test asserted only the REQUEST BODY, which its own title claimed was
  // not the point — a session that never reappears under UNTRACKED is the bug, and the request
  // landing proves nothing about that. So this drives the whole loop: detach, the overview
  // refetch, and the card arriving in UNTRACKED with its mission stamp gone.
  await stub(page);
  let held = true; // the overview stamps the card until the mission releases it
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        ...OVERVIEW,
        cards: [
          {
            id: "claude:aaa",
            engine: "claude",
            title: "the held session",
            cwd: "/repo",
            last_activity: T - 60,
            live: true,
            state: "in_flight",
            intervention_required: false,
            intervention_reason: "",
            // THE OWNERSHIP FACT, stamped server-side. UNTRACKED filters on it.
            mission_id: held ? "msn_1" : null,
          },
        ],
      },
    }),
  );
  await mockMissions(page, { missions: missionList([HELD_ROW]) });
  // The mission's OWN roster has to follow the detach too — producer-faithful, because the console
  // excludes a selected mission's roster from UNTRACKED (`heldExtra`), so a mock that keeps
  // returning the released session hides the very thing this test is about.
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    return r.fulfill({
      json: {
        ...HELD,
        sessions: held ? [{ session_key: "claude:aaa", removed_at: null }] : [],
        events: [],
        events_next_seq: null,
      },
    });
  });
  await page.route("**/api/missions/*/context", (r) =>
    r.fulfill({
      json: {
        id: "msn_1",
        project_id: "p1",
        cwd: "/repo",
        sessions: held ? [{ session_key: "claude:aaa", removed_at: null }] : [],
        git: null,
        git_error: null,
      },
    }),
  );
  const posts: unknown[] = [];
  await page.route("**/api/missions/*/detach", (r) => {
    posts.push(r.request().postDataJSON());
    held = false;
    return r.fulfill({ json: { ...HELD, sessions: [] } });
  });

  await page.goto("/pulse");
  await goToObjectives(page);
  const release = page.getByTestId("session-detach").first();
  await expect(release).toBeVisible();
  await release.click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toEqual({ session_key: "claude:aaa" });

  // …and the actual point: the released session is offered as untracked again. Without the
  // overview refresh the card keeps its old `mission_id` and stays invisible until the outer poll.
  await selectUntracked(page);
  await expect(page.getByTestId("untracked-session")).toHaveCount(1);
});

test("CLOSING a mission also puts its sessions back in UNTRACKED", async ({
  page,
}) => {
  // Reaching a terminal state releases every session the mission holds, server-side — so it is a
  // membership change exactly as detach is, and the same refresh has to happen.
  await stub(page);
  let held = true;
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        ...OVERVIEW,
        cards: [
          {
            id: "claude:aaa",
            engine: "claude",
            title: "the held session",
            cwd: "/repo",
            last_activity: T - 60,
            live: true,
            state: "in_flight",
            intervention_required: false,
            intervention_reason: "",
            mission_id: held ? "msn_1" : null,
          },
        ],
      },
    }),
  );
  await mockMissions(page, { missions: missionList([HELD_ROW]) });
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    return r.fulfill({
      json: {
        ...HELD,
        state: held ? "running" : "done",
        outcome: held ? null : "done",
        // Reaching a terminal state sets `removed_at` on every held session, server-side.
        sessions: held ? [{ session_key: "claude:aaa", removed_at: null }] : [],
        events: [],
        events_next_seq: null,
      },
    });
  });
  await page.route("**/api/missions/*/state", (r) => {
    held = false;
    return r.fulfill({
      json: { ...HELD, state: "done", outcome: "done", sessions: [] },
    });
  });

  await page.goto("/pulse");
  // MARK DONE confirms — closing releases every session the mission holds, so the
  // first tap only asks (#896 review 6, finding 2).
  await page.getByTestId("mission-done").click();
  await page.getByTestId("mission-done").click();
  await selectUntracked(page);
  await expect(page.getByTestId("untracked-session")).toHaveCount(1);
});

test("a CREATE that lands after CANCEL refreshes the rail but does not steal the SELECTION", async ({
  page,
}) => {
  // The first version of this asserted only that the composer was back in ASK mode — which CANCEL
  // does on its own, so it passed against the unfenced code. The hijack is observable in the
  // SELECTION: the console switching to a mission the operator has already walked away from.
  await stub(page);
  // A LOOSE SESSION, so the UNTRACKED view exists in the rail — that is where NEW MISSION lives
  // now that the mission body's composer is the durable one (#890).
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        ...OVERVIEW,
        cards: [
          {
            id: "claude:loose",
            engine: "claude",
            title: "A session no mission owns",
            cwd: "/repo",
            last_activity: T - 60,
            live: true,
            state: "working",
            mission_id: null,
          },
        ],
      },
    }),
  );
  const BRAVO = missionRow({ id: "msn_b", title: "Bravo" });
  const lists: string[] = [];
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (r.request().method() === "POST" && u.pathname === "/api/missions") {
      await new Promise((res) => setTimeout(res, 900)); // held open, so CANCEL lands first
      return r.fulfill({
        status: 201,
        json: {
          ...MISSION,
          id: "msn_new",
          title: "Newly made",
          state: "draft",
          sessions: [],
        },
      });
    }
    if (u.pathname === "/api/missions") {
      lists.push(u.search);
      return r.fulfill({ json: missionList([BRAVO]) });
    }
    return r.fallback();
  });
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: id === "msn_new" ? "Newly made" : "Bravo",
        sessions: [],
        events: [],
        events_next_seq: null,
      },
    });
  });

  await page.goto("/pulse");
  // NEW MISSION LIVES IN THE UNTRACKED VIEW. The mission body's composer is the DURABLE one
  // (#890): its turns are that mission's own rows, and creating a different mission is not one
  // of them. So the create starts here, and the operator then moves to a mission — which is the
  // same sequence this test was always about, entered from where the control actually is.
  await selectUntracked(page);
  await closeRail(page);
  await page.getByTestId("composer-mode-new").click();
  await page
    .getByTestId("new-mission-instruction")
    .fill("start then change my mind");
  await page.getByTestId("new-mission-project").selectOption("p1");
  const before = lists.length;
  await page.getByTestId("new-mission-start").click();
  await page.getByTestId("new-mission-cancel").click();
  await selectMission(page, "Bravo");
  await expectMissionSelected(page, "Bravo");

  await page.waitForTimeout(1500);
  // The rail WAS refreshed — the mission exists, and hiding it would be worse than showing it …
  expect(lists.length).toBeGreaterThan(before);
  // … but the operator is still where they were.
  await expect(page.getByTestId("mission-console")).toContainText("Bravo");
  await expect(page.getByTestId("mission-console")).not.toContainText(
    "Newly made",
  );
});

for (const state of ["done", "failed", "abandoned"] as const) {
  test(`a ${state} mission is a finished record — no objective edits, no stand-down, no release`, async ({
    page,
  }) => {
    await stub(page);
    await mockMissions(page, {
      missions: missionList([missionRow({ state })]),
      mission: {
        ...HELD,
        state,
        outcome: state === "abandoned" ? "abandoned" : state,
        events: [],
        events_next_seq: null,
        supervisor: {
          objectives: [
            {
              key: "checks",
              title: "Checks are green",
              gate: true,
              state: "pending",
              met: false,
              episode: 1,
              stood_down: false,
              spent: 0,
              remaining: 3,
              may_nudge: true,
              unreadable: false,
              indeterminate: false,
              live: 0,
              terminal: false,
              why_not: "",
            },
          ],
          likely_done: false,
          unmet_gates: 1,
          checked_at: T,
        },
      },
      objectives: {
        objectives: [
          {
            mission_id: "msn_1",
            key: "pr_open",
            ord: 0,
            title: "A PR is open",
            probe: "forge_pr",
            probe_args: null,
            gate: true,
            state: "pending",
            met_at: null,
            observed: null,
            source: "playbook",
          },
        ],
      },
      context: {
        id: "msn_1",
        project_id: "p1",
        cwd: "/repo",
        sessions: [{ session_key: "claude:aaa", removed_at: null }],
        git: null,
        git_error: null,
      },
    });
    await page.goto("/pulse");
    await goToObjectives(page);
    await expect(page.getByTestId("objective").first()).toBeVisible();

    // The routes would still take these writes; withdrawing the control is the point. Reopening
    // from the lifecycle bar is the honest way to edit a closed mission — it says so on screen.
    await expect(page.getByTestId("objective-waive")).toHaveCount(0);
    await expect(page.getByTestId("objective-drop")).toHaveCount(0);
    await expect(page.getByTestId("objective-add")).toHaveCount(0);
    await expect(page.getByTestId("objective-rename")).toHaveCount(0);
    await expect(page.getByTestId("objective-stand-down")).toHaveCount(0);
    await expect(page.getByTestId("session-detach")).toHaveCount(0);
  });
}

/** A rail with more rows than one page, so LOAD MORE is real (#896 review 4, finding 2). */
function page1(rows: number) {
  return Array.from({ length: rows }, (_, i) =>
    missionRow({ id: `msn_p1_${i}`, title: `First ${i}` }),
  );
}

test("a LOAD MORE that lands first does not make the refresh that followed it lose", async ({
  page,
}) => {
  await stub(page);
  // One of the two orderings, and the one the generation counter already handled: an append
  // issued BEFORE a mutation's refresh and arriving AFTER it lost on `gen < appliedGen` even
  // when it carried a generation of its own. Pinned anyway, because the fix changes what an
  // append is fenced ON — and a property that holds today by coincidence is the kind that stops
  // holding silently. The MIRROR ordering below is the one that was broken.
  const FIRST = page1(50);
  const GONE = missionRow({
    id: "msn_gone",
    title: "Vanishing",
    state: "done",
  });
  let archived = false;
  let releaseAppend: (() => void) | null = null;
  const appendHeld = new Promise<void>((r) => (releaseAppend = r));

  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions" || r.request().method() !== "GET")
      return r.fallback();
    const offset = Number(u.searchParams.get("offset") ?? 0);
    if (offset > 0) {
      // THE APPEND, held open until the refresh has been issued and answered.
      await appendHeld;
      return r.fulfill({
        json: {
          ...missionList([missionRow({ id: "msn_p2", title: "Second page" })]),
          total: 51,
        },
      });
    }
    const first = archived ? FIRST : [GONE, ...FIRST.slice(0, 49)];
    return r.fulfill({ json: { ...missionList(first), total: 51 } });
  });
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: id === "msn_gone" ? "Vanishing" : "First",
        state: id === "msn_gone" ? "done" : "running",
        outcome: id === "msn_gone" ? "done" : null,
        sessions: [],
        events: [],
        events_next_seq: null,
      },
    });
  });
  await page.route("**/api/missions/*/archive", (r) => {
    archived = true;
    return r.fulfill({ json: { mission: { ...MISSION, id: "msn_gone" } } });
  });

  await page.goto("/pulse");
  await selectMission(page, "Vanishing");
  await expectMissionSelected(page, "Vanishing");

  // 1. Ask for the next page. It is held.
  await openMissionRail(page);
  await page.locator('[data-testid="rail-load-more"]:visible').click();
  await closeRail(page);

  // 2. Archive, which issues the authoritative refresh. It answers immediately.
  await page.getByTestId("mission-archive").click();
  await page.getByTestId("mission-archive").click();
  await expect
    .poll(async () =>
      (await railRows(page).filter({ hasText: "Vanishing" }).count()) > 0
        ? "there"
        : "gone",
    )
    .toBe("gone");

  // 3. NOW the append lands. It must not resurrect the row the refresh dropped.
  releaseAppend?.();
  await page.waitForTimeout(400);
  await openMissionRail(page);
  await expect(railRows(page).filter({ hasText: "Vanishing" })).toHaveCount(0);
});

test("a refresh that lands first is not undone by the LOAD MORE it overtook", async ({
  page,
}) => {
  await stub(page);
  // The mirror ordering. The refresh is authoritative whenever it lands, so a page issued
  // against the list it replaced must not paste rows from that older list back on the end.
  const FIRST = page1(50);
  const GONE = missionRow({
    id: "msn_gone2",
    title: "Vanishing",
    state: "done",
  });
  // A DISTINCT id from anything the refresh returns. A page that repeats an id already in the
  // rail gives React duplicate keys, and the orphaned DOM node it leaves behind looks exactly
  // like the bug under test — a test that cannot tell those apart proves nothing.
  const STALE = missionRow({ id: "msn_stale", title: "Stale page" });
  let archived = false;
  let releaseRefresh: (() => void) | null = null;
  const refreshHeld = new Promise<void>((r) => (releaseRefresh = r));

  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions" || r.request().method() !== "GET")
      return r.fallback();
    const offset = Number(u.searchParams.get("offset") ?? 0);
    if (offset > 0)
      return r.fulfill({ json: { ...missionList([STALE]), total: 51 } });
    if (archived) await refreshHeld;
    const first = archived ? FIRST : [GONE, ...FIRST.slice(0, 49)];
    return r.fulfill({ json: { ...missionList(first), total: 51 } });
  });
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: id === "msn_gone2" ? "Vanishing" : "First",
        state: id === "msn_gone2" ? "done" : "running",
        outcome: id === "msn_gone2" ? "done" : null,
        sessions: [],
        events: [],
        events_next_seq: null,
      },
    });
  });
  await page.route("**/api/missions/*/archive", (r) => {
    archived = true;
    return r.fulfill({ json: { mission: { ...MISSION, id: "msn_gone2" } } });
  });

  await page.goto("/pulse");
  await selectMission(page, "Vanishing");
  await page.getByTestId("mission-archive").click();
  await page.getByTestId("mission-archive").click();

  // The refresh is in flight. Ask for the next page — its answer belongs to the list the
  // refresh is about to replace.
  await openMissionRail(page);
  await page.locator('[data-testid="rail-load-more"]:visible').click();
  await closeRail(page);
  await page.waitForTimeout(300);

  releaseRefresh?.();

  // The authoritative answer wins whenever it lands: the archived row is gone AND the page that
  // was appended to the list it replaced does not survive it.
  await openMissionRail(page);
  await expect(railRows(page).filter({ hasText: "Vanishing" })).toHaveCount(0);
  await expect(railRows(page).filter({ hasText: "Stale page" })).toHaveCount(0);
});

test("a FAILED archive leaves you on the mission whose error you were just handed", async ({
  page,
}) => {
  // #896 review 5, finding 3. `act()` forwarded `{scopeChanged: true}` from its `finally`, so a
  // REJECTED archive told the console the mission had left this rail — and the console clears the
  // selection on that. The operator is moved away from the very mission whose refusal they need
  // to read. The re-read is what a failure needs; the consequences of a change that did not
  // happen are not.
  await stub(page);
  const KEEP = missionRow({ id: "msn_keep", title: "Keep" });
  const GO = missionRow({ id: "msn_go", title: "Going", state: "done" });
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions" || r.request().method() !== "GET")
      return r.fallback();
    return r.fulfill({ json: missionList([KEEP, GO]) });
  });
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: id === "msn_go" ? "Going" : "Keep",
        state: id === "msn_go" ? "done" : "running",
        outcome: id === "msn_go" ? "done" : null,
        sessions: [],
        events: [],
        events_next_seq: null,
      },
    });
  });
  await page.route("**/api/missions/*/archive", (r) =>
    r.fulfill({
      status: 409,
      json: { detail: "another worker is archiving this mission" },
    }),
  );

  await page.goto("/pulse");
  await selectMission(page, "Going");
  // ASSERTED ON THE TITLE, never on the console: the rail is INSIDE the console and it contains
  // the word "Going" as a row, so a console-level match passes whether the operator was moved or
  // not. That is the shape that made the first version of this test green against the very code
  // it was written to fail. The header is what says which mission the body is showing.
  await expect(page.getByTestId("console-title")).toHaveText("Going");

  await page.getByTestId("mission-archive").click();
  await page.getByTestId("mission-archive").click();

  // The server's own reason is shown…
  await expect(page.getByTestId("mission-console")).toContainText(
    "another worker",
  );
  // …ON the mission it is about. A cleared selection falls back to auto-selecting `Keep`.
  await expect(page.getByTestId("console-title")).toHaveText("Going");
});

test("a decision started in UNTRACKED does not paint its refusal over a mission", async ({
  page,
}) => {
  // #896 review 5, finding 4. The UNTRACKED view is not inside the keyed mission body, so
  // selecting a mission does not unmount it — and `ActionRow`'s settled-record 409 calls the
  // console's note surface directly. Same rule as every other late outcome; it had just never
  // been applied on this path.
  await stub(page);
  const A = missionRow({ id: "msn_a", title: "Alpha", session_keys: [] });
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        ...OVERVIEW,
        cards: [
          {
            id: "claude:loose",
            engine: "claude",
            title: "A session no mission owns",
            cwd: "/repo",
            last_activity: T - 60,
            live: true,
            state: "needs_you",
            mission_id: null,
            pending_action: {
              id: "act_1",
              verb: "answer",
              title: "Switch the default model",
              session_id: "claude:loose",
              state: "proposed",
              confidence: 0.9,
              rationale: "the model is wrong",
            },
          },
        ],
      },
    }),
  );
  await mockMissions(page, {
    missions: missionList([A]),
    mission: {
      ...MISSION,
      id: "msn_a",
      title: "Alpha",
      events: [],
      events_next_seq: null,
    },
  });

  let release: (() => void) | null = null;
  const held = new Promise<void>((r) => (release = r));
  await page.route("**/api/pulse/actions/*/approve", async (r) => {
    await held;
    // THE SETTLED RECORD IS THE BODY ITSELF, not a `record` key inside it. `mutateJson` carries
    // the whole parsed body as `ApiError.record`, and `ActionRow` shape-checks `id`/`state` on
    // it — so a nested shape takes the INLINE-note branch and never reaches the parent callback
    // this test is about. That is a documented false-green in this repo and it caught this test
    // once already.
    return r.fulfill({
      status: 409,
      json: {
        detail: "that action was already settled",
        id: "act_1",
        state: "stale",
        verb: "answer",
        title: "Switch the default model",
        session_id: "claude:loose",
      },
    });
  });

  await page.goto("/pulse");
  await selectUntracked(page);
  await page
    .getByRole("button", { name: /^approve$/i })
    .first()
    .click();

  // …and the operator moves to a mission while it is in flight.
  await selectMission(page, "Alpha");
  await expectMissionSelected(page, "Alpha");

  release?.();
  await page.waitForTimeout(500);
  await expect(page.getByTestId("mission-console")).not.toContainText(
    "already settled",
  );
});

test("a late CLOSE for a mission you left still refreshes UNTRACKED", async ({
  page,
}) => {
  // #896 review 6, finding 1. Fencing the whole callback dropped BOTH effects, and only one of
  // them is mission-local. Closing releases the mission's sessions SERVER-SIDE, and the overview
  // cards keep their old `mission_id` until something re-reads them — so suppressing the
  // membership refresh leaves those sessions in neither the roster nor UNTRACKED until the outer
  // poll happens to run. The operator having navigated away does not un-release them.
  await stub(page);
  const A = missionRow({
    id: "msn_a",
    title: "Alpha",
    session_keys: ["claude:aaa"],
  });
  const B = missionRow({ id: "msn_b", title: "Bravo", session_keys: [] });
  let closed = false;
  let overviewReads = 0;

  await page.route(/\/api\/pulse$/, (r) => {
    overviewReads += 1;
    return r.fulfill({
      json: {
        ...OVERVIEW,
        cards: [
          {
            id: "claude:aaa",
            engine: "claude",
            title: "Alpha's session",
            cwd: "/repo",
            last_activity: T - 60,
            live: true,
            state: "working",
            // The release is a SERVER fact, and the card only learns it on a re-read.
            mission_id: closed ? null : "msn_a",
          },
        ],
      },
    });
  });
  await mockMissions(page, { missions: missionList([A, B]) });
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: id === "msn_a" ? "Alpha" : "Bravo",
        state: id === "msn_a" && closed ? "done" : "running",
        outcome: id === "msn_a" && closed ? "done" : null,
        sessions:
          id === "msn_a" && !closed
            ? [{ session_key: "claude:aaa", removed_at: null }]
            : [],
        events: [],
        events_next_seq: null,
      },
    });
  });

  let release: (() => void) | null = null;
  const held = new Promise<void>((r) => (release = r));
  await page.route("**/api/missions/*/state", async (r) => {
    await held;
    closed = true;
    return r.fulfill({
      json: { ...MISSION, id: "msn_a", state: "done", outcome: "done" },
    });
  });

  await page.goto("/pulse");
  await selectMission(page, "Alpha");
  // MARK DONE now confirms (finding 2), so it takes two taps.
  await page.getByTestId("mission-done").click();
  await page.getByTestId("mission-done").click();

  // …and the operator moves on while it is in flight.
  await selectMission(page, "Bravo");
  await expect(page.getByTestId("console-title")).toHaveText("Bravo");

  const before = overviewReads;
  release?.();
  // The overview is re-read even though the mission that changed is not the one on screen.
  await expect
    .poll(() => overviewReads, { timeout: 10_000 })
    .toBeGreaterThan(before);
  // …and the released session is back under UNTRACKED.
  await openMissionRail(page);
  await expect(
    page.locator('[data-testid="rail-untracked-view"]:visible'),
  ).toBeVisible();
});

test("UNARCHIVE offers RECORD ONLY beside RESTART AGENTS, and each sends its own choice", async ({
  page,
}) => {
  // UNARCHIVE relaunches every archived session from its transcript. On one click, with nothing
  // on screen saying so, an operator who read it as "put the record back" could start several
  // agents — with the cost and the side effects that implies (#896 review 6, finding 4). The
  // controls exist; the browser had never pressed either of them (#896 review 7, finding 3).
  await stub(page);
  const SHELVED = missionRow({
    id: "msn_arch",
    title: "Shelved",
    archived_at: T - 900,
  });
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions" || r.request().method() !== "GET")
      return r.fallback();
    return r.fulfill({
      json: missionList(
        u.searchParams.get("archived") === "1" ? [SHELVED] : [],
      ),
    });
  });
  await page.route(/\/api\/missions\/msn_arch(\?.*)?$/, (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    return r.fulfill({
      json: {
        ...MISSION,
        id: "msn_arch",
        title: "Shelved",
        archived_at: T - 900,
        sessions: [],
        events: [],
        events_next_seq: null,
      },
    });
  });
  const bodies: unknown[] = [];
  await page.route("**/api/missions/*/unarchive", (r) => {
    bodies.push(r.request().postDataJSON());
    return r.fulfill({ json: { mission: { ...MISSION, id: "msn_arch" } } });
  });

  await page.goto("/pulse");
  await ready(page);
  await openMissionRail(page);
  await page.locator('[data-testid="rail-scope"]:visible').first().click();
  await railRows(page).filter({ hasText: "Shelved" }).click();
  await closeRail(page);

  // It CONFIRMS, and the confirmation says what the dangerous choice does.
  await page.getByTestId("mission-unarchive").click();
  await expect(page.getByTestId("mission-confirm-unarchive")).toContainText(
    "RESTART",
  );
  await expect(bodies).toHaveLength(0);

  // The SAFE choice is offered, and it asks for the record only.
  await page.getByTestId("mission-unarchive-record").click();
  await expect.poll(() => bodies.length).toBe(1);
  expect((bodies[0] as Record<string, unknown>).sessions).toBe(false);

  // …and the other one asks for the relaunch, explicitly. Reached from the Archived rail again:
  // a successful unarchive now lands the operator in the scope the mission moved INTO, which is
  // its own test below — this one is about the two choices, so it goes back and presses the other.
  await openMissionRail(page);
  await page.locator('[data-testid="rail-scope"]:visible').first().click();
  await railRows(page).filter({ hasText: "Shelved" }).click();
  await closeRail(page);
  await page.getByTestId("mission-unarchive").click();
  await page.getByTestId("mission-unarchive-sessions").click();
  await expect.poll(() => bodies.length).toBe(2);
  expect((bodies[1] as Record<string, unknown>).sessions).toBe(true);
});

test("a SUPERSEDED list failure does not paint a store outage over newer rows", async ({
  page,
}) => {
  // `live` is the effect's LIFETIME, which is a different question from ownership (#896 review 9,
  // finding 3). `reload()` is a callback, not the effect — so a mutation's refresh can install
  // fresh rows and clear the error while the MOUNT request is still open and still `live`. When
  // that one finally rejects it painted "the mission store could not be read" over data that had
  // just arrived.
  //
  // NEW MISSION is the trigger because it needs no painted rail: the mount read is held, so
  // there is nothing to select.
  await stub(page);
  await page.route("**/api/projects**", (r) =>
    r.fulfill({ json: { projects: [{ id: "p1", name: "repo" }] } }),
  );
  const A = missionRow({ id: "msn_a", title: "Alpha" });
  let releaseMount: (() => void) | null = null;
  let calls = 0;
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions") return r.fallback();
    if (r.request().method() === "POST")
      return r.fulfill({ status: 201, json: { ...MISSION, id: "msn_a" } });
    calls += 1;
    if (calls === 1) {
      // The MOUNT read, held open — and then failed, after the refresh below has landed.
      await new Promise<void>((res) => {
        releaseMount = res;
      });
      return r.fulfill({ status: 500, json: { detail: "boom" } });
    }
    return r.fulfill({ json: missionList([A]) });
  });
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    return r.fulfill({
      json: {
        ...MISSION,
        id: "msn_a",
        title: "Alpha",
        sessions: [],
        events: [],
        events_next_seq: null,
      },
    });
  });

  await page.goto("/pulse");
  await expect(page.getByTestId("mission-console")).toBeVisible();
  await expect.poll(() => calls).toBe(1);

  // A create issues the authoritative refresh, which answers with real rows.
  await page.getByTestId("composer-mode-new").click();
  await page.getByTestId("new-mission-instruction").fill("ship it");
  await page.getByTestId("new-mission-project").selectOption("p1");
  await page.getByTestId("new-mission-start").click();
  await expect.poll(() => calls).toBeGreaterThan(1);
  await expect.poll(() => railCount(page)).toBeGreaterThan(0);

  // …and only THEN does the superseded mount request fail.
  releaseMount?.();
  await page.waitForTimeout(400);

  await expect(page.getByTestId("rail-store-error")).toHaveCount(0);
  await expect.poll(() => railCount(page)).toBeGreaterThan(0);
});

test("a late ADOPT failure is not filed against the mission you moved to", async ({
  page,
}) => {
  // The refusal is a fact about THIS attempt — "already held by mission X" — and the console note
  // outlives the view it was raised in. Started in UNTRACKED and resolved after the operator
  // selected mission B, an unfenced error appeared over B with nothing to say which mission it
  // was about (#896 review 9, finding 4).
  await stub(page);
  const A = missionRow({ id: "msn_a", title: "Alpha", session_keys: [] });
  const B = missionRow({ id: "msn_b", title: "Bravo", session_keys: [] });
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        ...OVERVIEW,
        cards: [
          {
            id: "claude:loose",
            engine: "claude",
            title: "A session no mission owns",
            cwd: "/repo",
            last_activity: T - 60,
            live: true,
            state: "working",
            // Membership KNOWN and empty — `undefined` means "could not be read", and the
            // console correctly withholds ADOPT in that case.
            mission_id: null,
          },
        ],
      },
    }),
  );
  await mockMissions(page, {
    missions: missionList([A, B]),
  });
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: id === "msn_b" ? "Bravo" : "Alpha",
        sessions: [],
        events: [],
        events_next_seq: null,
      },
    });
  });
  let releaseAdopt: (() => void) | null = null;
  const heldAdopt = new Promise<void>((r) => (releaseAdopt = r));
  await page.route("**/api/missions/*/adopt", async (r) => {
    await heldAdopt;
    return r.fulfill({
      status: 409,
      json: { detail: "claude:loose is already held by mission msn_z" },
    });
  });

  await page.goto("/pulse");

  // Start the adoption from UNTRACKED…
  await selectUntracked(page);
  await closeRail(page);
  await page.locator('[data-testid="rail-adopt"]:visible').first().click();

  // …then move to mission B while it is still in flight.
  await selectMission(page, "Bravo");
  await expectMissionSelected(page, "Bravo");

  releaseAdopt?.();
  await page.waitForTimeout(400);

  // B's pane says nothing about a session it was never asked to adopt.
  await expect(page.getByTestId("mission-console")).not.toContainText(
    "already held by mission msn_z",
  );
});

test("a stale PAGINATION cleanup cannot unlock a newer request", async ({
  page,
}) => {
  // The scope is not ownership: it ROUND-TRIPS (#896 review 9, finding 5). A1 can still be in
  // flight while the operator visits Archived and comes back and A2 starts — then A1 settles,
  // sees its own scope again, and clears A2's busy flag. A3 is then allowed at the same offset,
  // shares A2's base and generation, and both append the same rows.
  //
  // BOTH page requests are held, because the unlock only matters while A2 is still running — a
  // test that let A2 finish would be asserting against a busy flag that had legitimately
  // cleared, and would pass against the unfixed code.
  await stub(page);
  const page1 = Array.from({ length: 50 }, (_, i) =>
    missionRow({ id: `msn_${i}`, title: `Mission ${i}` }),
  );
  const page2 = [missionRow({ id: "msn_50", title: "Mission 50" })];

  let releaseA1: (() => void) | null = null;
  let releaseA2: (() => void) | null = null;
  const heldA1 = new Promise<void>((r) => (releaseA1 = r));
  const heldA2 = new Promise<void>((r) => (releaseA2 = r));
  let pageCalls = 0;
  const fulfilled: number[] = [];
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions" || r.request().method() !== "GET")
      return r.fallback();
    const offset = Number(u.searchParams.get("offset") ?? 0);
    const archived = u.searchParams.get("archived") === "1";
    if (archived) return r.fulfill({ json: { ...missionList([]), total: 0 } });
    if (offset === 0)
      return r.fulfill({ json: { ...missionList(page1), total: 51 } });
    pageCalls += 1;
    const mine = pageCalls;
    if (mine === 1) await heldA1;
    if (mine === 2) await heldA2;
    fulfilled.push(mine);
    return r.fulfill({ json: { ...missionList(page2), total: 51 } });
  });

  await page.goto("/pulse");
  await ready(page);
  await openMissionRail(page);

  // A1 — held.
  await page.locator('[data-testid="rail-load-more"]:visible').click();
  await expect.poll(() => pageCalls).toBe(1);

  // The scope round-trip, which returns `archivedRef` to exactly what A1 captured.
  await page.locator('[data-testid="rail-scope"]:visible').first().click();
  await page.locator('[data-testid="rail-scope"]:visible').first().click();
  await expect
    .poll(async () =>
      page.locator('[data-testid="rail-mission"]:visible').count(),
    )
    .toBeGreaterThan(1);

  // A2 starts, and is also held.
  await page.locator('[data-testid="rail-load-more"]:visible').click();
  await expect.poll(() => pageCalls).toBe(2);

  // …and only now does A1 settle. It must not unlock A2.
  releaseA1?.();
  await page.waitForTimeout(300);

  // A2 IS STILL IN FLIGHT, so the control stays disabled — asserted on the button rather than by
  // clicking it, because with the fix a click correctly blocks for ever and with the bug it
  // issues a THIRD request at A2's offset, so the two would append the same rows.
  // A2 IS STILL IN FLIGHT, so the control stays disabled — asserted on the button rather than by
  // clicking it, because with the fix a click correctly blocks for ever, while with the bug it
  // issues a THIRD request at A2's offset and the two then append the same rows.
  expect(fulfilled).toEqual([1]); // A1 really did settle; the assertion below is about its effect
  await expect(
    page.locator('[data-testid="rail-load-more"]:visible'),
  ).toBeDisabled();
  expect(pageCalls).toBe(2);

  releaseA2?.();
});

test("a CURRENT list failure says the store could not be read", async ({
  page,
}) => {
  // The ordinary case, and the one the notice exists for — suppressed by fencing failures on
  // `appliedGen`, which only advances on SUCCESS (#896 review 10, finding 1). The rail then
  // painted "Nothing tracked yet" over a store that would not answer, which is exactly the
  // conflation this notice was written to prevent.
  await stub(page);
  await page.route("**/api/missions**", (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions" || r.request().method() !== "GET")
      return r.fallback();
    return r.fulfill({ status: 500, json: { detail: "boom" } });
  });

  await page.goto("/pulse");
  await ready(page);
  // On a phone the rail is a DRAWER, so the notice is only on screen once it is opened. Asserting
  // on `:visible` without opening it fails for the layout rather than for the fence.
  await openMissionRail(page);
  await expect(
    page.locator('[data-testid="rail-store-error"]:visible').first(),
  ).toBeVisible();
  // …and it does NOT say "you have no missions".
  await expect(page.locator('[data-testid="rail-no-missions"]')).toHaveCount(0);
});

test("UNARCHIVE lands you in the scope the mission moved INTO, not the one it left", async ({
  page,
}) => {
  // Archive and unarchive are the same event in opposite directions, and the console answered
  // both by re-reading the rail on screen (#896 review 10, finding 6). From Archived that
  // re-reads Archived — the one list the mission has just left — so the operator was left in a
  // scope where the mission they had just restored does not belong, looking at a rail that keeps
  // the row until the server drops it.
  //
  // Asserted on the PAINTED result, not on the request body: the unarchive POST was already
  // correct, and a spec that stops at it passes against exactly this bug.
  await stub(page);
  const SHELVED = missionRow({
    id: "msn_arch",
    title: "Shelved",
    archived_at: T - 900,
  });
  const RESTORED = missionRow({ id: "msn_arch", title: "Shelved" });
  let restored = false;
  const scopes: string[] = [];
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions" || r.request().method() !== "GET")
      return r.fallback();
    const archived = u.searchParams.get("archived") === "1";
    scopes.push(archived ? "archived" : "active");
    return r.fulfill({
      json: missionList(
        archived ? (restored ? [] : [SHELVED]) : restored ? [RESTORED] : [],
      ),
    });
  });
  await page.route(/\/api\/missions\/msn_arch(\?.*)?$/, (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    return r.fulfill({
      json: {
        ...MISSION,
        id: "msn_arch",
        title: "Shelved",
        ...(restored ? {} : { archived_at: T - 900 }),
        sessions: [],
        events: [],
        events_next_seq: null,
      },
    });
  });
  await page.route("**/api/missions/*/unarchive", (r) => {
    restored = true;
    return r.fulfill({ json: { mission: { ...MISSION, id: "msn_arch" } } });
  });

  await page.goto("/pulse");
  await ready(page);
  await openMissionRail(page);
  await page.locator('[data-testid="rail-scope"]:visible').first().click();
  await expect(
    page.locator('[data-testid="rail-scope"]:visible').first(),
  ).toHaveText("Show active");
  await railRows(page).filter({ hasText: "Shelved" }).click();
  await closeRail(page);

  await page.getByTestId("mission-unarchive").click();
  await page.getByTestId("mission-unarchive-record").click();

  // THE SCOPE ON SCREEN IS ACTIVE. The toggle offers the way back to Archived, which it only
  // does from the active rail.
  const scope = page.locator('[data-testid="rail-scope"]:visible').first();
  await openMissionRail(page);
  await expect(scope).toHaveText("Show archived");
  // …and the ACTIVE rail is what was read for it, carrying the restored row.
  await expect(railRows(page).filter({ hasText: "Shelved" })).toHaveCount(1);
  expect(scopes.at(-1)).toBe("active");
});

test("a refusal from the visit you LEFT stays gone after you come back to that mission", async ({
  page,
}) => {
  // #896 review 10, finding 3. The fence asked "is Alpha the mission on screen?", and after
  // Alpha → Bravo → Alpha the honest answer is yes — so a refusal raised in the FIRST visit was
  // admitted into the second, telling the operator that something they tried before they stepped
  // away had just failed, over a mission whose state has since been re-read.
  //
  // An id can be true again; a mount cannot be re-entered. The body is keyed on the mission, so
  // the second Alpha is a different mount, and the fence is that mount's own life.
  await stub(page);

  const A = missionRow({
    id: "msn_a",
    title: "Alpha",
    session_keys: ["claude:aaa"],
  });
  const B = missionRow({ id: "msn_b", title: "Bravo", session_keys: [] });
  await mockMissions(page, { missions: missionList([A, B]) });
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: id === "msn_a" ? "Alpha" : "Bravo",
        state: "running",
        sessions:
          id === "msn_a"
            ? [{ session_key: "claude:aaa", removed_at: null }]
            : [],
        events: [],
        events_next_seq: null,
      },
    });
  });

  let release: (() => void) | undefined;
  const held = new Promise<void>((r) => (release = r));
  await page.route("**/api/missions/*/state", async (r) => {
    await held;
    return r.fulfill({
      status: 409,
      json: { detail: "mission msn_a is no longer running" },
    });
  });

  await page.goto("/pulse");
  await selectMission(page, "Alpha");
  await expect(page.getByTestId("mission-state")).toHaveText("running");
  await page.getByTestId("mission-done").click();
  await page.getByTestId("mission-done").click();

  // Away …
  await selectMission(page, "Bravo");
  await expectMissionSelected(page, "Bravo");
  // … AND BACK. This is the whole test: by now the id fence says "Alpha is current" again.
  await selectMission(page, "Alpha");
  await expect(page.getByTestId("mission-state")).toHaveText("running");

  release?.();

  // Waiting a beat so a note that WOULD land has landed — asserting immediately passes against
  // the reviewed shape too.
  await page.waitForTimeout(750);
  await expect(page.getByTestId("console-note")).toHaveCount(0);
});

test("a REFUSED adopt reconciles the overview it just proved stale", async ({
  page,
}) => {
  // #896 review 10, finding 5. The 409 this path exists to report NAMES another mission as the
  // holder — which is positive evidence that the card the operator adopted from, still showing
  // `mission_id: null`, is the stale picture. Refreshing the overview only on success left
  // UNTRACKED asserting an ownership the server had just denied, until the outer poll ran.
  await stub(page);
  const A = missionRow({ id: "msn_a", title: "Alpha", session_keys: [] });
  let overviewReads = 0;
  await page.route(/\/api\/pulse$/, (r) => {
    overviewReads += 1;
    return r.fulfill({
      json: {
        ...OVERVIEW,
        cards: [
          {
            id: "claude:loose",
            engine: "claude",
            title: "A session no mission owns",
            cwd: "/repo",
            last_activity: T - 60,
            live: true,
            state: "working",
            // The claim the server is about to contradict.
            mission_id: null,
          },
        ],
      },
    });
  });
  await mockMissions(page, { missions: missionList([A]) });
  await page.route("**/api/missions/*/adopt", (r) =>
    r.fulfill({
      status: 409,
      json: { detail: "claude:loose is already held by mission msn_z" },
    }),
  );

  await page.goto("/pulse");
  await selectUntracked(page);
  await closeRail(page);
  await expect.poll(() => overviewReads).toBeGreaterThan(0);
  const before = overviewReads;
  await page.locator('[data-testid="rail-adopt"]:visible').first().click();

  // The refusal still reaches the operator, naming the holder …
  await expect(page.getByTestId("console-note")).toContainText("msn_z");
  // … AND the overview is re-read, because the card that offered ADOPT is the thing the server
  // just contradicted.
  await expect.poll(() => overviewReads).toBeGreaterThan(before);
});

test("THE #889 JOURNEY: create → objectives arrive → edit → adopt → BEGIN → confirm done → archive", async ({
  page,
}) => {
  // #896 review 10, finding 7. Every step below was already covered somewhere — but each in its
  // own fixture, each starting from a mission that was already in the state the step needed.
  // What was never asserted is that the states CONNECT: that the mission a create returns is the
  // one whose objectives arrive, that the objectives you edit belong to the mission you adopt a
  // session into, that BEGIN unlocks because of THAT adoption, and that the archive at the end
  // takes the same mission off the active rail.
  //
  // So this is one evolving server, not seven fixtures. The mocks hold real state and every
  // response is computed from it; a step that silently did nothing changes what the next step
  // sees, which is precisely what a per-step fixture cannot notice.
  await stub(page);

  const MID = "msn_j";
  const server = {
    created: false,
    objectivesReady: false,
    titles: ["Draft the adapter", "Open the PR"],
    state: "draft",
    sessions: [] as { session_key: string; removed_at: number | null }[],
    archived: false,
  };
  const patches: unknown[] = [];
  const adopts: unknown[] = [];

  const row = () => ({
    ...missionRow({
      id: MID,
      title: "Wire the adapter",
      state: server.state,
      session_keys: server.sessions.map((s) => s.session_key),
      ...(server.archived ? { archived_at: T - 5 } : {}),
    }),
  });
  const detail = () => ({
    ...MISSION,
    id: MID,
    title: "Wire the adapter",
    instruction: "Wire the adapter",
    state: server.state,
    sessions: server.sessions,
    archived_at: server.archived ? T - 5 : null,
    // THE SETTLEMENT #896 never exercised: the objectives producer is a background task, so a
    // freshly created mission legitimately has none for a moment and says `pending`.
    objectives_state: server.objectivesReady ? "done" : "pending",
    events: [],
    events_next_seq: null,
  });

  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        ...OVERVIEW,
        cards: [
          {
            id: "claude:worker",
            engine: "claude",
            title: "A session no mission owns",
            cwd: "/repo",
            last_activity: T - 60,
            live: true,
            state: "working",
            mission_id: server.sessions.length ? MID : null,
          },
        ],
      },
    }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({ json: { projects: [{ id: "p1", name: "the-app" }] } }),
  );
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    const method = r.request().method();
    if (u.pathname === "/api/missions" && method === "POST") {
      server.created = true;
      return r.fulfill({ json: detail() });
    }
    if (u.pathname === "/api/missions" && method === "GET") {
      const wantArchived = u.searchParams.get("archived") === "1";
      const rows =
        server.created && server.archived === wantArchived ? [row()] : [];
      return r.fulfill({ json: missionList(rows) });
    }
    return r.fallback();
  });
  await page.route(/\/api\/missions\/msn_j(\?.*)?$/, (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    return r.fulfill({ json: detail() });
  });
  await page.route("**/api/missions/*/context", (r) =>
    r.fulfill({
      json: {
        id: MID,
        project_id: "p1",
        cwd: "/repo",
        sessions: server.sessions,
        git: null,
        git_error: null,
      },
    }),
  );
  await page.route("**/api/missions/*/objectives", async (r) => {
    if (r.request().method() === "PATCH") {
      const body = r.request().postDataJSON() as {
        ops: { op: string; key: string; title?: string }[];
      };
      patches.push(body);
      for (const op of body.ops) {
        const i = Number(op.key.replace("o", ""));
        if (op.op === "retitle" && op.title) server.titles[i] = op.title;
      }
      return r.fulfill({ json: { objectives: [] } });
    }
    return r.fulfill({
      json: {
        objectives: server.objectivesReady
          ? server.titles.map((title, i) => ({
              mission_id: MID,
              key: `o${i}`,
              ord: i,
              title,
              probe: "none",
              probe_args: null,
              gate: false,
              state: "pending",
              met_at: null,
              observed: null,
              source: "model",
            }))
          : [],
      },
    });
  });
  await page.route("**/api/missions/*/adopt", (r) => {
    adopts.push(r.request().postDataJSON());
    server.sessions = [{ session_key: "claude:worker", removed_at: null }];
    return r.fulfill({ json: detail() });
  });
  await page.route("**/api/missions/*/state", (r) => {
    const b = r.request().postDataJSON() as { from: string; to: string };
    if (b.from !== server.state)
      return r.fulfill({
        status: 409,
        json: { detail: `mission is ${server.state}, not ${b.from}` },
      });
    server.state = b.to;
    return r.fulfill({ json: detail() });
  });
  await page.route("**/api/missions/*/archive", (r) => {
    server.archived = true;
    return r.fulfill({ json: { mission: detail() } });
  });

  await page.goto("/pulse");
  await ready(page);

  // ── 1. CREATE ────────────────────────────────────────────────────────────────────────────
  await page.getByTestId("composer-mode-new").click();
  await page.getByTestId("new-mission-instruction").fill("Wire the adapter");
  await page.getByTestId("new-mission-project").selectOption("p1");
  await page.getByTestId("new-mission-start").click();
  // The console SELECTS what the create returned — not what a later list happens to carry.
  await expect(page.getByTestId("mission-console")).toContainText(
    "Wire the adapter",
  );

  // ── 2. OBJECTIVES ARRIVE ─────────────────────────────────────────────────────────────────
  // `pending` is a list the server is about to change, and the console says so rather than
  // showing an empty one as a settled answer.
  await goToObjectives(page);
  // SCOPED TO THE PANE. At >=1400px the objectives are rendered TWICE — the stop's content and
  // the persistent detail column — so an unscoped locator is a strict-mode violation on desktop
  // and a passing test on mobile only.
  const pane = page.getByTestId("pane");
  await expect(pane.getByTestId("objectives-pending")).toBeVisible();
  server.objectivesReady = true;
  // No reload, no click: the console's own bounded poll is what ends the wait.
  await expect(pane.getByTestId("objective")).toHaveCount(2, {
    timeout: 20_000,
  });
  await expect(pane.getByTestId("objectives")).toContainText("Open the PR");

  // ── 3. EDIT ──────────────────────────────────────────────────────────────────────────────
  await pane.getByTestId("objective-rename").nth(1).click();
  await pane.getByTestId("objective-rename-input").fill("Open the PR upstream");
  await pane.getByTestId("objective-rename-save").click();
  await expect.poll(() => patches.length).toBe(1);
  expect(patches[0]).toEqual({
    ops: [{ op: "retitle", key: "o1", title: "Open the PR upstream" }],
  });
  await expect(pane.getByTestId("objectives")).toContainText(
    "Open the PR upstream",
  );

  // ── 4. ADOPT ─────────────────────────────────────────────────────────────────────────────
  // BEGIN is refused without one, and the button SAYS so before the adoption.
  await page.getByTestId("stop-thread").click();
  await expect(page.getByTestId("mission-plan")).toBeVisible();
  await page.getByTestId("mission-plan").click();
  await expect(page.getByTestId("mission-state")).toHaveText("planned");
  await expect(page.getByTestId("mission-begin")).toBeDisabled();

  await selectUntracked(page);
  await closeRail(page);
  await page.locator('[data-testid="rail-adopt"]:visible').first().click();
  await expect.poll(() => adopts.length).toBe(1);
  expect(adopts[0]).toEqual({ session_key: "claude:worker" });

  // ── 5. BEGIN, then CONFIRM DONE ──────────────────────────────────────────────────────────
  await selectMission(page, "Wire the adapter");
  await expect(page.getByTestId("mission-begin")).toBeEnabled();
  await page.getByTestId("mission-begin").click();
  await expect(page.getByTestId("mission-state")).toHaveText("running");

  // Closing releases every session the mission holds, so it CONFIRMS.
  await page.getByTestId("mission-done").click();
  await expect(page.getByTestId("mission-done")).toContainText(/confirm/i);
  expect(server.state).toBe("running");
  await page.getByTestId("mission-done").click();
  await expect(page.getByTestId("mission-state")).toHaveText("done");

  // ── 6. ARCHIVE ───────────────────────────────────────────────────────────────────────────
  await page.getByTestId("mission-archive").click();
  await expect(page.getByTestId("mission-confirm-archive")).toBeVisible();
  await page.getByTestId("mission-archive").click();

  // THE ACTIVE RAIL LOSES IT — and the console does not go on rendering a body for a mission the
  // rail no longer lists, which is the rail-and-body mismatch #889 pins.
  await expect.poll(() => railCount(page)).toBe(0);
  await expect(page.getByTestId("mission-lifecycle")).toHaveCount(0);
  // …and it is exactly one toggle away, in Archived.
  await toggleScope(page);
  await expect.poll(() => railCount(page)).toBe(1);
});

test("a settled-action refusal from a PREVIOUS untracked visit stays gone", async ({
  page,
}) => {
  // #896 review 11, finding 2. `noteIfUntracked` compared the sentinel ID, which is true again
  // the moment the operator comes back — so UNTRACKED → mission B → UNTRACKED admitted a 409
  // raised two views ago into a list that has since been re-read: "Not sent — already settled",
  // about something the operator did before they stepped out.
  //
  // `ActionRow` reports at RESOLUTION time and takes no token, so the visit is captured in the
  // callback identity it holds. Red against an id-only fence.
  await stub(page);
  const A = missionRow({ id: "msn_a", title: "Alpha", session_keys: [] });
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        ...OVERVIEW,
        cards: [
          {
            id: "claude:loose",
            engine: "claude",
            title: "A session no mission owns",
            cwd: "/repo",
            last_activity: T - 60,
            live: true,
            state: "working",
            mission_id: null,
            pending_action: {
              id: "act_1",
              // A REAL DELIVERING VERB. `ActionRow` renders Approve only for a verb that has
              // something to deliver — an `escalate` row is a question, not a proposal — so a
              // fixture with an invented verb has no button and the test would pass vacuously.
              verb: "continue",
              state: "proposed",
              session_id: "claude:loose",
              engine: "claude",
              title: "A session no mission owns",
              rationale: "it looks stuck",
              confidence: 0.9,
              ts: T - 600,
              expires_at: T + 1800,
              tier: "suggest",
            },
          },
        ],
      },
    }),
  );
  await mockMissions(page, { missions: missionList([A]) });

  let release: (() => void) | undefined;
  const held = new Promise<void>((r) => (release = r));
  await page.route("**/api/pulse/actions/*/approve", async (r) => {
    await held;
    // THE SETTLED RECORD, not just a `detail`. `ActionRow` raises the explanation to the CONSOLE
    // only when the 409 carries one — a bare `{detail}` keeps the note on the row itself, which
    // would make this test pass without ever reaching the fence it is about.
    return r.fulfill({
      status: 409,
      json: {
        detail: "that action was already settled",
        id: "act_1",
        state: "expired",
        verb: "continue",
        session_id: "claude:loose",
      },
    });
  });

  await page.goto("/pulse");
  await selectUntracked(page);
  await closeRail(page);
  await page
    .getByRole("button", { name: /^approve$/i })
    .first()
    .click();

  // Away …
  await selectMission(page, "Alpha");
  await expectMissionSelected(page, "Alpha");
  // … AND BACK. By now the id fence says "UNTRACKED is current" again.
  await selectUntracked(page);
  await closeRail(page);

  release?.();
  // A beat, so a note that WOULD land has landed.
  await page.waitForTimeout(750);
  await expect(page.getByTestId("console-note")).toHaveCount(0);
});

test("an ARCHIVED mission leaves the Active rail even when the refresh that follows FAILS", async ({
  page,
}) => {
  // #896 review 11, finding 3. `reload()` swallows its failures and keeps the list it has —
  // deliberately, because a failed refresh must not empty a rail. But a SUCCESSFUL archive whose
  // refresh then failed left the archived row in the Active list, `autoSelected` picked it
  // straight back up, and the operator was looking at an archived mission's body under an Active
  // rail, with nothing on screen saying so and no later poll to repair it.
  //
  // Red against a console that waits for the re-read to tell it what it already knows.
  await stub(page);
  const ONLY = missionRow({ id: "msn_a", title: "Alpha", state: "done" });
  let listsFail = false;
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions" || r.request().method() !== "GET")
      return r.fallback();
    if (listsFail) return r.fulfill({ status: 500, json: { detail: "boom" } });
    return r.fulfill({ json: missionList([ONLY]) });
  });
  await page.route(/\/api\/missions\/msn_a(\?.*)?$/, (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    return r.fulfill({
      json: {
        ...MISSION,
        id: "msn_a",
        title: "Alpha",
        state: "done",
        closed_at: T - 10,
        sessions: [],
        events: [],
        events_next_seq: null,
      },
    });
  });
  await page.route("**/api/missions/*/archive", (r) => {
    // The ARCHIVE succeeds; only the refresh that follows it fails.
    listsFail = true;
    return r.fulfill({ json: { mission: { ...MISSION, id: "msn_a" } } });
  });

  await page.goto("/pulse");
  await selectMission(page, "Alpha");
  await expect(page.getByTestId("mission-state")).toContainText("done");

  await page.getByTestId("mission-archive").click();
  await page.getByTestId("mission-archive").click();

  // THE ROW IS GONE FROM THE ACTIVE RAIL, on the server's word rather than on a re-read that
  // never landed …
  await expect.poll(() => railCount(page)).toBe(0);
  // … and nothing re-selects it, so no archived body is rendered under an Active rail.
  await expect(page.getByTestId("mission-lifecycle")).toHaveCount(0);
});

test("a LATE archive still takes its row out of the Active rail after you have moved on", async ({
  page,
}) => {
  // #896 review 12, finding 1. `movedId` rode on the VIEW-LOCAL half of the callback, so it was
  // suppressed once the archived mission's body had unmounted — which is precisely the case that
  // needs it: the operator has walked away, the rail is what they are looking at, and the
  // refresh that was supposed to reconcile it can fail.
  //
  // Held A → navigate to B → A settles → the list read 500s. Red against a row removal that
  // requires `scopeChanged`, which is the fenced half.
  await stub(page);
  const A = missionRow({ id: "msn_a", title: "Alpha", state: "done" });
  const B = missionRow({ id: "msn_b", title: "Bravo", state: "done" });
  let listsFail = false;
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions" || r.request().method() !== "GET")
      return r.fallback();
    if (listsFail) return r.fulfill({ status: 500, json: { detail: "boom" } });
    return r.fulfill({ json: missionList([A, B]) });
  });
  await page.route(/\/api\/missions\/msn_[ab](\?.*)?$/, (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: id === "msn_a" ? "Alpha" : "Bravo",
        state: "done",
        closed_at: T - 10,
        sessions: [],
        events: [],
        events_next_seq: null,
      },
    });
  });

  let release: (() => void) | undefined;
  const held = new Promise<void>((r) => (release = r));
  await page.route("**/api/missions/*/archive", async (r) => {
    await held;
    listsFail = true; // the refresh that follows the archive fails
    return r.fulfill({ json: { mission: { ...MISSION, id: "msn_a" } } });
  });

  await page.goto("/pulse");
  await selectMission(page, "Alpha");
  await expect(page.getByTestId("mission-state")).toContainText("done");
  await page.getByTestId("mission-archive").click();
  await page.getByTestId("mission-archive").click();

  // THE OPERATOR MOVES ON while the archive is still in flight — which unmounts Alpha's body.
  await selectMission(page, "Bravo");
  await expectMissionSelected(page, "Bravo");

  release?.();
  await page.waitForTimeout(750);

  // Alpha is GONE from the Active rail, on the server's word, and Bravo is untouched.
  const rows = railRows(page);
  await openMissionRail(page);
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("Bravo");
});

test("LOAD MORE dedupes, and still advances past what it consumed", async ({
  page,
}) => {
  // #896 reviews 15 and 17, finding 1 / finding 2. LOAD MORE asks for an OFFSET into a list the
  // server orders by `updated_at DESC`, so a mission touched since the previous page has moved to
  // the front and shifted everything after it down one — and the page then begins with a row the
  // rail already has.
  //
  // Two things go wrong, and the second was introduced by fixing the first:
  //
  // * keeping both copies made `rows.length` reach `total` while a mission was missing, so LOAD
  //   MORE disappeared and that mission was unreachable without a reload;
  // * deduping fixed the count and broke the CURSOR, because the next offset was taken from the
  //   deduplicated rendered count. That asks for a row already on screen, so the rail sticks one
  //   short for ever — the same mission unreachable, by the other route.
  //
  // Red against either: no dedupe leaves a duplicate on screen; a rendered-count offset asks 199.
  await stub(page);
  const ALL = Array.from({ length: 300 }, (_, i) =>
    missionRow({ id: `msn_${i}`, title: `M${i}`, state: "done" }),
  );
  const offsets: number[] = [];
  // The tear stops once the operator asks for a fresh read — a real interleaving happens once,
  // and a stub that lies for ever could only ever assert that nothing helps.
  let tearing = true;
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions" || r.request().method() !== "GET")
      return r.fallback();
    if (u.searchParams.get("archived") === "1")
      return r.fulfill({ json: missionList([]) });
    const offset = Number(u.searchParams.get("offset") ?? 0);
    const limit = Number(u.searchParams.get("limit") ?? 100);
    offsets.push(offset);
    // EVERY page after the first is served from a set one row has jumped to the front of —
    // exactly what another tab touching a mission leaves behind.
    const src =
      offset === 0 || !tearing
        ? ALL
        : [ALL[ALL.length - 1], ...ALL.slice(0, ALL.length - 1)];
    return r.fulfill({
      json: {
        ...missionList(
          src.slice(offset, offset + limit),
          null,
          // The digest names the ORDERED set, so a shifted list is a different one — which is
          // what the server would send. Held faithful so the assertions below still rest on
          // dedupe and the cursor rather than passing for free on the digest.
          src === ALL ? "snap" : "shifted",
        ),
        total: ALL.length,
      },
    });
  });

  await page.goto("/pulse");
  await ready(page);
  await openMissionRail(page);
  await expect.poll(() => railRows(page).count()).toBe(100);

  // PAGE 1 overlaps page 0 by one row, so 100 + 100 rows arrive and 199 are shown.
  await page.locator('[data-testid="rail-load-more"]:visible').first().click();
  await expect.poll(() => railRows(page).count()).toBe(199);
  expect(await railDupes(page)).toEqual([]);

  // …AND THE NEXT PAGE ASKS AT THE CURSOR, not at the rendered count: 200 rows have been
  // consumed, 199 are on screen. Asking for 199 re-requests the tail row and the rail never
  // grows again.
  offsets.length = 0;
  await page.locator('[data-testid="rail-load-more"]:visible').first().click();
  await expect.poll(() => offsets).toContain(200);
  expect(offsets).not.toContain(199);
  // …and it really did advance: another page of rows arrived.
  await expect.poll(() => railRows(page).count()).toBeGreaterThan(199);
  expect(await railDupes(page)).toEqual([]);

  // EVERY SERVER ROW IS CONSUMED AND THE RAIL IS STILL SHORT (#896 review 18): 300 consumed, 299
  // rendered, total 300. LOAD MORE must be GONE — it asks by offset, and there is no offset left
  // to ask at, so leaving it on screen is a control that looks like the way to the missing
  // mission and does nothing when tapped.
  await expect.poll(() => railRows(page).count()).toBe(299);
  await expect(
    page.locator('[data-testid="rail-load-more"]:visible'),
  ).toHaveCount(0);

  // …and the recovery that CAN close the hole is offered instead, and closes it.
  const reRead = page.locator('[data-testid="rail-re-read"]:visible').first();
  await expect(reRead).toBeVisible();
  tearing = false;
  await reRead.click();
  await expect.poll(() => railRows(page).count()).toBe(300);
  expect(await railDupes(page)).toEqual([]);
  await expect(
    page.locator('[data-testid="rail-re-read"]:visible'),
  ).toHaveCount(0);
});

test("a REMOVAL between pages leaves no duplicate, and the rail says so anyway", async ({
  page,
}) => {
  // #896 review 19, finding 1. This is the case every earlier defence misses.
  //
  // A REORDER produces a duplicate, and dedupe catches it. A REMOVAL produces neither: archive
  // M50 between page 0 and page 1 and the second page starts one row late, so the rail ends up
  // with 199 unique rows, `total` 199, `consumed` 199 — count-consistent, duplicate-free, LOAD
  // MORE correctly gone — while it still shows the archived M50 and has permanently lost M100.
  // Nothing on screen says a thing. A duplicate is proof of tearing; the absence of one is not
  // proof of a snapshot.
  //
  // So the server names the ordered set each page was cut from, and a page cut from a different
  // one is not a continuation of this read. Red against a rail that decides tearing by looking
  // for a duplicate.
  await stub(page);
  const ALL = Array.from({ length: 200 }, (_, i) =>
    missionRow({ id: `msn_${i}`, title: `M${i}`, state: "done" }),
  );
  // …and the archive settles for good, so the re-read has something correct to find. A stub that
  // lied for ever could only prove that nothing helps.
  const AFTER = ALL.filter((m) => m.id !== "msn_50");
  let removed = false;
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions" || r.request().method() !== "GET")
      return r.fallback();
    if (u.searchParams.get("archived") === "1")
      return r.fulfill({ json: missionList([]) });
    const offset = Number(u.searchParams.get("offset") ?? 0);
    const limit = Number(u.searchParams.get("limit") ?? 100);
    const src = removed ? AFTER : ALL;
    // ANOTHER CLIENT ARCHIVES M50 the moment page 0 has been served.
    if (offset === 0 && !removed && limit <= 100) removed = true;
    return r.fulfill({
      json: {
        ...missionList(
          src.slice(offset, offset + limit),
          null,
          src === ALL ? "before" : "after",
        ),
        total: src.length,
      },
    });
  });

  await page.goto("/pulse");
  await ready(page);
  await openMissionRail(page);
  await expect.poll(() => railRows(page).count()).toBe(100);

  await page.locator('[data-testid="rail-load-more"]:visible').first().click();
  // THE STATE THAT USED TO BE SILENT: 199 rows, no duplicate, nothing left to page for.
  await expect.poll(() => railRows(page).count()).toBe(199);
  expect(await railDupes(page)).toEqual([]);
  await expect(
    page.locator('[data-testid="rail-load-more"]:visible'),
  ).toHaveCount(0);
  // …and it is WRONG in both directions, which is what the counts cannot show.
  const titles = async () =>
    (await railRows(page).allInnerTexts()).map(
      (t) => (t.match(/M\d+\b/) || [""])[0],
    );
  expect(await titles()).toContain("M50");
  expect(await titles()).not.toContain("M100");

  // So the rail offers the one recovery that can close an interior hole, and it closes it.
  const reRead = page.locator('[data-testid="rail-re-read"]:visible').first();
  await expect(reRead).toBeVisible();
  await reRead.click();
  // POLLED ON THE CONTENT, not the count: the rail already HAS 199 rows, so a count assertion
  // passes before the re-read has landed and would prove nothing about it.
  await expect.poll(async () => (await titles()).includes("M100")).toBe(true);
  expect(await titles()).not.toContain("M50");
  await expect.poll(() => railRows(page).count()).toBe(199);
  expect(await railDupes(page)).toEqual([]);
  await expect(
    page.locator('[data-testid="rail-re-read"]:visible'),
  ).toHaveCount(0);
});

test("a rail refresh reads ONE snapshot, so a deletion between pages cannot hide a row", async ({
  page,
}) => {
  // #896 review 16, finding 1. Deduplicating page overlaps catches a REORDER, because a repeated
  // id cannot happen inside one snapshot. It cannot catch a REMOVAL: another client archives a
  // mission between page 0 and page 1, offset 100 then starts one row later, and the merged rail
  // has 199 unique rows — no duplicate, `total` 199, LOAD MORE hidden — while still holding the
  // stale archived row and permanently missing the one that was pushed past the boundary.
  //
  // A duplicate is proof of tearing; the absence of one is not proof of a snapshot. The server
  // clamps a page at 200, so the whole open window comes back from ONE statement over ONE
  // snapshot — and this asserts that the refresh asks for it that way rather than stitching.
  //
  // Red against a refresh that pages: it issues two offset requests and hides LOAD MORE.
  await stub(page);
  const ALL = Array.from({ length: 200 }, (_, i) =>
    missionRow({ id: `msn_${i}`, title: `M${i}`, state: "done" }),
  );
  let archived = false;
  const asked: { offset: number; limit: number }[] = [];
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions" || r.request().method() !== "GET")
      return r.fallback();
    if (u.searchParams.get("archived") === "1")
      return r.fulfill({ json: missionList([]) });
    const offset = Number(u.searchParams.get("offset") ?? 0);
    const limit = Number(u.searchParams.get("limit") ?? 100);
    asked.push({ offset, limit });
    // THE DELETION, mid-refresh: a mission in the FIRST page is archived by somebody else, so a
    // second request at offset 100 would start one row late and miss M100 entirely.
    const live = archived ? ALL.filter((m) => m.id !== "msn_50") : ALL;
    return r.fulfill({
      json: {
        ...missionList(live.slice(offset, offset + limit)),
        total: live.length,
      },
    });
  });
  await page.route(/\/api\/missions\/msn_\d+(\?.*)?$/, (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: `M${String(id).split("_")[1]}`,
        state: "done",
        closed_at: T - 10,
        sessions: [],
        events: [],
        events_next_seq: null,
      },
    });
  });
  await page.route("**/api/missions/*/archive", (r) => {
    archived = true;
    return r.fulfill({ json: { mission: { ...MISSION, id: "msn_0" } } });
  });

  await page.goto("/pulse");
  await ready(page);
  await openMissionRail(page);
  await expect.poll(() => railRows(page).count()).toBe(100);
  await page.locator('[data-testid="rail-load-more"]:visible').first().click();
  await expect.poll(() => railRows(page).count()).toBe(200);

  // An ARCHIVE is the cheapest thing that makes the console re-read what is open.
  await railRows(page).first().click();
  await closeRail(page);
  await expect(page.getByTestId("mission-state")).toContainText("done");
  asked.length = 0;
  await page.getByTestId("mission-archive").click();
  await page.getByTestId("mission-archive").click();
  await page.waitForTimeout(1200);

  // THE REFRESH ASKED ONCE. Every offset request here is a page that could have been stitched to
  // another, which is the thing that cannot be proved consistent.
  expect(asked.filter((a) => a.offset > 0)).toEqual([]);
  expect(asked.length).toBeGreaterThan(0);

  // Re-opened to LOOK, through the shell (#940). This site reused the `opener` binding from the
  // top of the test rather than declaring its own — invisible to eslint and to tsc, because no
  // tsconfig here includes `e2e/`. The browser found it: `ReferenceError: opener is not defined`.
  await openMissionRail(page);
  // …and the row that a stitched read would have lost is on screen.
  await expect(railRows(page).filter({ hasText: "M100" })).toHaveCount(1);
  await expect(railRows(page).filter({ hasText: "M50" })).toHaveCount(0);
});

test("a LATE archive does not delete the row from the ARCHIVED rail it just entered", async ({
  page,
}) => {
  // #896 review 14. The row removal is global — it has to be, because the operator has usually
  // walked away by the time a held archive settles — but it was applied to whichever rail was on
  // screen, with nothing saying which rail the mission had LEFT.
  //
  // So: archive A from Active, hold the response, switch to Archived where a fresh list
  // correctly installs A, then release the held response while its follow-up read fails. The
  // late `movedId` filtered A out of the ARCHIVED rail — the one place it belongs — and the
  // mission was unreachable until a reload.
  //
  // Red against a removal that does not compare the destination against the rail on screen.
  await stub(page);
  const A = missionRow({ id: "msn_a", title: "Alpha", state: "done" });
  const B = missionRow({ id: "msn_b", title: "Bravo", state: "done" });
  let archived = false;
  let listsFail = false;
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions" || r.request().method() !== "GET")
      return r.fallback();
    if (listsFail) return r.fulfill({ status: 500, json: { detail: "boom" } });
    // The server has COMMITTED the archive, so both lists already agree about where A is.
    if (u.searchParams.get("archived") === "1")
      return r.fulfill({ json: missionList(archived ? [A] : []) });
    return r.fulfill({ json: missionList(archived ? [B] : [A, B]) });
  });
  await page.route(/\/api\/missions\/msn_[ab](\?.*)?$/, (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: id === "msn_a" ? "Alpha" : "Bravo",
        state: "done",
        closed_at: T - 10,
        archived_at: id === "msn_a" && archived ? T - 5 : null,
        sessions: [],
        events: [],
        events_next_seq: null,
      },
    });
  });

  let release: (() => void) | undefined;
  const held = new Promise<void>((r) => (release = r));
  await page.route("**/api/missions/*/archive", async (r) => {
    archived = true; // the server COMMITS immediately …
    await held; //     … and the response is held
    return r.fulfill({ json: { mission: { ...MISSION, id: "msn_a" } } });
  });

  await page.goto("/pulse");
  await selectMission(page, "Alpha");
  await expect(page.getByTestId("mission-state")).toContainText("done");
  await page.getByTestId("mission-archive").click();
  await page.getByTestId("mission-archive").click();

  // THE OPERATOR SWITCHES TO ARCHIVED while the archive is still in flight, and the fresh
  // archived list correctly carries Alpha.
  await toggleScope(page);
  await openMissionRail(page);
  await expect(railRows(page)).toHaveCount(1);
  await expect(railRows(page).first()).toContainText("Alpha");

  listsFail = true; // the reload that follows the late response fails
  release?.();
  await page.waitForTimeout(750);

  // ALPHA IS STILL THERE. The rail on screen is the DESTINATION, so there is nothing to remove.
  await expect(railRows(page)).toHaveCount(1);
  await expect(railRows(page).first()).toContainText("Alpha");
});

test("a LATE archive cannot decrement a total a newer list has already reconciled", async ({
  page,
}) => {
  // #896 review 13, finding 1. The rows and the count were two `useState`s, and the local removal
  // decremented the count whether or not it removed anything. So a removal a NEWER list had
  // already made was counted twice — and `rows.length >= total` then hid LOAD MORE over a mission
  // that is still there, unreachable until a reload.
  //
  // 102 active missions; A's archive is held; a newer list installs the first 100 of the
  // remaining 101; the old response is released and its follow-up reload fails. Red against an
  // unconditional decrement.
  await stub(page);
  const ALL = Array.from({ length: 102 }, (_, i) =>
    missionRow({ id: `msn_${i}`, title: `M${i}`, state: "done" }),
  );
  let archived = false;
  let listsFail = false;
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions" || r.request().method() !== "GET")
      return r.fallback();
    if (listsFail) return r.fulfill({ status: 500, json: { detail: "boom" } });
    if (u.searchParams.get("archived") === "1")
      return r.fulfill({ json: missionList([]) });
    // Once A is archived the server has 101, and a page is 100.
    const live = archived ? ALL.slice(1) : ALL;
    const offset = Number(u.searchParams.get("offset") ?? 0);
    return r.fulfill({
      json: {
        ...missionList(live.slice(offset, offset + 100)),
        total: live.length,
      },
    });
  });
  await page.route(/\/api\/missions\/msn_\d+(\?.*)?$/, (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: `M${String(id).split("_")[1]}`,
        state: "done",
        closed_at: T - 10,
        sessions: [],
        events: [],
        events_next_seq: null,
      },
    });
  });

  let release: (() => void) | undefined;
  const held = new Promise<void>((r) => (release = r));
  await page.route("**/api/missions/*/archive", async (r) => {
    archived = true; // the server COMMITS immediately …
    await held; //     … and the response is held
    return r.fulfill({ json: { mission: { ...MISSION, id: "msn_0" } } });
  });

  await page.goto("/pulse");
  // The FIRST row is M0, which is the one the mock archives. Selected by position rather than by
  // text: a rail row carries its state and its time too, so an anchored title match finds nothing.
  await ready(page);
  await openMissionRail(page);
  await railRows(page).first().click();
  await closeRail(page);
  // The first rail row IS M0, and clicking it selects it. Asserted on the row rather than on the
  // console for the same reason as `expectMissionSelected`: the rail is no longer inside the
  // console (#935), and "the console mentions the word M0" was never the property under test.
  await expectMissionSelected(page, "M0");
  await expect(page.getByTestId("mission-state")).toContainText("done");
  await page.getByTestId("mission-archive").click();
  await page.getByTestId("mission-archive").click();

  // A NEWER LIST INSTALLS — the scope toggle is the cheapest way to force one — and it already
  // excludes the archived mission, so 100 of 101 are shown.
  await toggleScope(page);
  await toggleScope(page);
  await openMissionRail(page);
  await expect.poll(() => railRows(page).count()).toBe(100);

  listsFail = true; // the reload that follows the late response fails
  release?.();
  await page.waitForTimeout(750);

  // LOAD MORE IS STILL THERE. 100 shown of 101, so the 101st is reachable — an unconditional
  // decrement made it 100 of 100 and the control disappeared.
  // `:visible` — on a phone the rail is rendered twice (inline, hidden by CSS, and in the
  // drawer), so an unscoped locator is a strict-mode violation there and a pass on desktop only.
  await expect(
    page.locator('[data-testid="rail-load-more"]:visible').first(),
  ).toBeVisible();
});

const LOOSE_CARD = {
  id: "claude:loose",
  engine: "claude",
  title: "A session no mission owns",
  cwd: "/repo",
  last_activity: T - 60,
  live: true,
  state: "working",
  mission_id: null,
};

/** The UNTRACKED view with one loose session and whichever missions the case is about. */
async function untrackedWith(page: Page, rows: unknown[], adopts: string[]) {
  await stub(page);
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { ...OVERVIEW, cards: [LOOSE_CARD] } }),
  );
  await mockMissions(page, { missions: missionList(rows) });
  await page.route("**/api/missions/*/adopt", async (r) => {
    adopts.push(new URL(r.request().url()).pathname);
    return r.fulfill({ json: { ...MISSION, sessions: [] } });
  });
  await page.goto("/pulse");
  await selectUntracked(page);
  await closeRail(page);
  return page.locator('[data-testid="rail-adopt"]:visible').first();
}

test("ADOPT offers nothing when every mission is CLOSED, and says why", async ({
  page,
}) => {
  // #896 review 20, finding 2. A terminal transition RELEASES the roster — that is what it is for
  // — so a `done` mission holds nothing and follows nothing through. The rail picked the first
  // UNARCHIVED row regardless of state, so it aimed ADOPT at a mission the store was always going
  // to refuse: the operator taps a live-looking control and gets a 409 for a reason the screen
  // never mentioned.
  //
  // The store refusal is the guarantee (the route tests own that). This is the affordance.
  //
  // Red against a target picked from `missions[0]` without reading the state.
  const adopts: string[] = [];
  const adopt = await untrackedWith(
    page,
    [missionRow({ id: "msn_done", title: "Alpha", state: "done" })],
    adopts,
  );
  await expect(adopt).toBeDisabled();
  await expect(adopt).toHaveAttribute("title", /closed/i);
  expect(adopts).toEqual([]);
});

test("ADOPT aims at the first mission that can hold work, not the first row", async ({
  page,
}) => {
  // The other half: a closed mission ahead of an eligible one must not shadow it. Red against the
  // same `missions[0]` pick — the request goes to `msn_done` and the server refuses it.
  const adopts: string[] = [];
  const adopt = await untrackedWith(
    page,
    [
      missionRow({ id: "msn_done", title: "Alpha", state: "done" }),
      missionRow({ id: "msn_open", title: "Bravo", state: "running" }),
    ],
    adopts,
  );
  await expect(adopt).toBeEnabled();
  await adopt.click();
  await expect.poll(() => adopts).toEqual(["/api/missions/msn_open/adopt"]);
});

test("waiving an objective refreshes the RAIL, not just the pane", async ({
  page,
}) => {
  // #896 review 23, finding 3. `needs_you` on the rail is DERIVED from objective/episode state,
  // so waiving, dropping, reordering or standing one down can clear the very thing it reads. The
  // mutation re-read only the mission DETAIL, so the pane went current while the rail kept saying
  // the mission needs you — with no later list poll to reconcile them.
  //
  // Red against a mutation that reloads the detail alone: the second list request never happens.
  await stub(page);
  const lists: string[] = [];
  let needsYou = true;
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (r.request().method() !== "GET") return r.fallback();
    if (u.pathname === "/api/missions") {
      lists.push(u.search);
      return r.fulfill({
        json: missionList([
          missionRow({ ...HELD_ROW, needs_you: needsYou, state: "running" }),
        ]),
      });
    }
    if (
      u.pathname.startsWith("/api/missions/") &&
      u.pathname.endsWith("/objectives")
    )
      return r.fulfill({
        json: {
          objectives: [
            {
              mission_id: HELD.id,
              key: "pr_open",
              ord: 0,
              title: "A PR is open",
              probe: null,
              probe_args: null,
              gate: true,
              state: "pending",
              met_at: null,
              observed: null,
              source: "playbook",
            },
          ],
        },
      });
    return r.fulfill({ json: { ...HELD, events: [], events_next_seq: null } });
  });
  await page.route("**/api/missions/*/objectives", async (r) => {
    if (r.request().method() !== "PATCH") return r.fallback();
    needsYou = false; // the server cleared it — the rail must be told
    return r.fulfill({ json: { objectives: [] } });
  });

  // The rail is a COLUMN on desktop and a DRAWER on the phone, so reading it needs the drawer
  // open on one project and not the other — and the objectives live behind a stop on the phone,
  // which the drawer covers. Open, read, close, act.
  const railNeedsYou = async () => {
    await openMissionRail(page);
    const n = await railRows(page)
      .first()
      .getByRole("img", { name: "Needs you" })
      .count();
    await closeRail(page);
    return n;
  };

  await page.goto("/pulse");
  await ready(page);
  // THE RAIL SAYS "NEEDS YOU" BEFORE THE MUTATION — the state the test is about.
  await expect.poll(railNeedsYou, { timeout: 10_000 }).toBe(1);

  await goToObjectives(page);
  await expect(page.getByTestId("objective").first()).toBeVisible();
  const before = lists.length;

  await page.getByTestId("objective-waive").first().click();

  // THE RAIL BECOMES CURRENT, not merely re-read (#896 review 24, finding 3). Counting requests
  // passes against a generation fence that receives the fresh row and drops it — which leaves the
  // stale attention projection painted, the exact defect this guards. So the assertion is on what
  // the operator sees.
  await expect
    .poll(() => lists.length, { timeout: 10_000 })
    .toBeGreaterThan(before);
  await expect.poll(railNeedsYou, { timeout: 10_000 }).toBe(0);
});

test("PLANNING a mission refreshes the RAIL, not just the pane", async ({
  page,
}) => {
  // #904 review 18, finding 2. The plan card was the ONE mutation surface handed `d.reload`
  // alone, which re-reads the mission DETAIL and nothing else. So planning left the pane showing
  // a proposal beside a rail row still saying `draft`, and a dispatch left the session it had
  // just attached sitting in UNTRACKED until the supervisor's next sweep. The rule is stated
  // forty lines above the call site and every other surface follows it.
  //
  // Asserted on WHAT THE RAIL SAYS, not on a second request being issued: a count passes against
  // a generation fence that receives the fresh row and drops it, which leaves the stale row
  // painted — the exact defect. Same reasoning as the waive regression above.
  //
  // Red against `onChanged={d.reload}`: the rail row still reads `draft`.
  await stub(page);
  let planned = false;
  const PLAN = {
    plan_id: "pln_a",
    mission_id: "msn_a",
    project_id: "p1",
    cwd: "/repo/the-app",
    engine: "claude",
    engine_reason: "it is a python repo",
    brief: "open a PR that does the thing",
    created_at: 1,
    project_options: [{ id: "p1", name: "the-app", cwd: "/repo/the-app" }],
    engine_options: [{ id: "claude", label: "claude" }],
  };
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (r.request().method() !== "GET") return r.fallback();
    if (u.pathname === "/api/missions")
      return r.fulfill({
        json: missionList([
          missionRow({
            id: "msn_a",
            title: "Alpha",
            state: planned ? "planned" : "draft",
          }),
        ]),
      });
    if (u.pathname.endsWith("/objectives"))
      return r.fulfill({ json: { objectives: [] } });
    return r.fulfill({
      json: {
        ...MISSION,
        id: "msn_a",
        title: "Alpha",
        state: planned ? "planned" : "draft",
        plan: planned ? PLAN : null,
        events: [],
        events_next_seq: null,
      },
    });
  });
  await page.route("**/api/missions/*/plan", async (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    planned = true; // the SERVER moved draft -> planned, as the real route does
    return r.fulfill({ json: PLAN });
  });

  // The rail is a column on desktop and a drawer on the phone: open, read, close. Read from the
  // status dot's ACCESSIBLE NAME, which is the mission's state verbatim — the meta line that also
  // carries it is inside a `min-width: 1100px` block, so its text is not the same on both
  // projects and an assertion on it would pass on one viewport and be meaningless on the other.
  const railState = async () => {
    await openMissionRail(page);
    const label = await railRows(page)
      .first()
      .getByRole("img")
      .getAttribute("aria-label");
    await closeRail(page);
    return label ?? "";
  };

  await page.goto("/pulse");
  await ready(page);
  await expect.poll(railState, { timeout: 10_000 }).toContain("draft");

  await expect(page.getByTestId("mission-plan-propose")).toBeVisible();
  await page.getByTestId("mission-plan-propose").click();

  // The PANE gets the proposal…
  await expect(page.getByTestId("mission-plan-brief")).toBeVisible();
  // …AND SO DOES THE RAIL, without waiting for a poll or a trip through another mission.
  await expect.poll(railState, { timeout: 10_000 }).toContain("planned");
});

test("a late DISPATCH failure cannot paint over the mission you moved to", async ({
  page,
}) => {
  // #904 review 17, finding 2 — the same defect as the question card below, in the component
  // with the LONGEST await on this screen. A dispatch launches a process, so the operator has
  // every reason to go and look at something else while it runs; the plan card was handed the
  // console-global callback instead of the per-mission one, so mission A's failure was rendered
  // on mission B's pane. The comment beside the fence claims every consumer takes it, which was
  // true of all of them but this one.
  //
  // Red against `onNote` passed raw: A's reason appears while Bravo is on screen.
  await stub(page);
  const A = missionRow({ id: "msn_a", title: "Alpha", state: "planned" });
  const B = missionRow({ id: "msn_b", title: "Bravo", state: "planned" });
  await mockMissions(page, { missions: missionList([A, B]) });
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: id === "msn_a" ? "Alpha" : "Bravo",
        state: "planned",
        events: [],
        events_next_seq: null,
        // DISPATCH is gated on a settled, non-empty checklist — an unattended agent with nothing
        // to check itself against is the thing #893 refuses to start — so the fixture has to
        // satisfy that before the button is even enabled.
        objectives_state: "done",
        objectives: [
          {
            mission_id: id,
            key: "pr_open",
            ord: 0,
            title: "A PR is open",
            probe: null,
            probe_args: null,
            gate: true,
            state: "pending",
            met_at: null,
            observed: null,
            source: "playbook",
          },
        ],
        // Only ALPHA carries a proposal, so the card under test belongs to the mission we leave.
        plan:
          id === "msn_a"
            ? {
                plan_id: "pln_a",
                mission_id: "msn_a",
                project_id: "p1",
                cwd: "/repo/the-app",
                engine: "claude",
                engine_reason: "it is a python repo",
                brief: "open a PR that does the thing",
                created_at: 1,
                project_options: [
                  { id: "p1", name: "the-app", cwd: "/repo/the-app" },
                ],
                engine_options: [{ id: "claude", label: "claude" }],
              }
            : null,
      },
    });
  });

  let release: (() => void) | undefined;
  const held = new Promise<void>((r) => (release = r));
  await page.route("**/api/missions/*/dispatch", async (r) => {
    await held;
    // A REAL FAILURE VERDICT, not an exception: `state !== "running"` is the branch that notes.
    return r.fulfill({
      json: {
        state: "failed",
        reason: "ALPHA-DISPATCH-FAILED",
        session_key: null,
      },
    });
  });

  await page.goto("/pulse");
  await selectMission(page, "Alpha");
  await expect(page.getByTestId("mission-plan-card")).toBeVisible();

  // Two taps: the first arms the confirmation, the second starts it — and it is held open.
  await page.getByTestId("mission-dispatch").click();
  await expect(page.getByTestId("mission-dispatch-confirm")).toBeVisible();
  await page.getByTestId("mission-dispatch").click();

  // Away, while the dispatch is still in flight.
  await selectMission(page, "Bravo");
  await expectMissionSelected(page, "Bravo");

  release?.();
  // A beat, so a note that WOULD land has landed.
  await page.waitForTimeout(750);
  await expect(page.getByTestId("console-note")).toHaveCount(0);
  await expect(page.getByTestId("mission-console")).not.toContainText(
    "ALPHA-DISPATCH-FAILED",
  );
});

test("a late QUESTION refusal cannot paint over the mission you moved to", async ({
  page,
}) => {
  // #896 review 23, finding 4. Every other late outcome on this page takes the per-mission note
  // fence; the question card was handed the raw callback, so mission A's refusal — a 409 for a
  // superseded question, or `applied_ok: false` — landed on the console-global note after the
  // operator had navigated to B. The claim beside the fence ("every consumer gets the one
  // fence") was very nearly true, which is the worst kind.
  //
  // Red against `onNote` passed raw: the refusal appears while Bravo is on screen.
  await stub(page);
  const A = missionRow({ id: "msn_a", title: "Alpha" });
  const B = missionRow({ id: "msn_b", title: "Bravo" });
  await mockMissions(page, { missions: missionList([A, B]) });
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: id === "msn_a" ? "Alpha" : "Bravo",
        state: "running",
        events: [],
        events_next_seq: null,
        question:
          id === "msn_a"
            ? {
                seq: 41,
                question: "Which of the two open PRs is this mission's?",
                objective: "pr_open",
                episode: 1,
                options: [
                  {
                    label: "the first one",
                    action: "note_answer",
                    consequence: "Records your choice and carries on.",
                    settling: false,
                  },
                  {
                    label: "the second one",
                    action: "note_answer",
                    consequence: "Records your choice and carries on.",
                    settling: false,
                  },
                ],
              }
            : null,
      },
    });
  });

  let release: (() => void) | undefined;
  const held = new Promise<void>((r) => (release = r));
  await page.route("**/api/missions/*/answer", async (r) => {
    await held;
    return r.fulfill({
      status: 409,
      json: { detail: "that question has been superseded" },
    });
  });

  await page.goto("/pulse");
  await selectMission(page, "Alpha");
  await expect(page.getByTestId("mission-question")).toBeVisible();
  await page.getByTestId("mission-question-option").first().click();

  // Away, while the answer is still in flight.
  await selectMission(page, "Bravo");
  await expectMissionSelected(page, "Bravo");

  release?.();
  // A beat, so a note that WOULD land has landed.
  await page.waitForTimeout(750);
  await expect(page.getByTestId("console-note")).toHaveCount(0);
});

test("a LATE mutation does not collapse the pages you opened while it was in flight", async ({
  page,
}) => {
  // #896 reviews 25 and 26, finding 1. `reload()` re-reads the pages the operator has OPENED, and
  // it took that count from the `missions.length` its own callback closed over. An async child
  // holds a `reload` across its await — the composer's create resolves and calls `onCreated` — so
  // if the operator loaded another page while the create was pending, the refresh asked for the
  // OLD window and replaced the wider rail with it. Every opened page gone, and a mission
  // selected from the later page with it.
  //
  // **THE LOAD MORE PAGE IS HELD, AND THE CREATE IS RELEASED WHILE IT IS** (review 26). The first
  // version of this test waited for the 200-row paint before releasing the create, which only
  // ever exercised the closure — a ref that MIRRORS the rendered length is already correct by
  // then. The gap is one paint wide and it is where the operator actually clicks: the ask is out,
  // the answer has not come back. Holding the page makes that gap the whole test rather than a
  // race against a mock's round trip, so the ordering cannot pass by being lucky.
  //
  // Red against a captured length AND against a rendered-length mirror: the rail falls back to
  // 100 rows in both.
  await stub(page);
  // A LOOSE SESSION, so the UNTRACKED view exists — that is where NEW MISSION lives once the rail
  // has missions in it (the mission body's composer is the durable one).
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { ...OVERVIEW, cards: [LOOSE_CARD] } }),
  );
  const ALL = Array.from({ length: 200 }, (_, i) =>
    missionRow({ id: `msn_${i}`, title: `M${i}`, state: "running" }),
  );
  let releaseCreate: (() => void) | null = null;
  const heldCreate = new Promise<void>((r) => (releaseCreate = r));
  let releasePage: (() => void) | null = null;
  const heldPage = new Promise<void>((r) => (releasePage = r));
  const asked: number[] = [];
  await mockMissions(page, {
    missions: async (q: URLSearchParams) => {
      const offset = Number(q.get("offset") ?? 0);
      const limit = Number(q.get("limit") ?? 100);
      asked.push(limit);
      // THE SECOND PAGE IS HELD. Only the append is an offset read; the refresh under test asks
      // for the whole window from 0, so it is never the request being held.
      if (offset > 0) await heldPage;
      return {
        ...missionList(ALL.slice(offset, offset + limit)),
        total: ALL.length,
      };
    },
  });
  // Registered AFTER `mockMissions` so it wins for this exact URL — and only for the POST, so the
  // detail and objectives reads keep the shapes their components expect.
  await page.route("**/api/missions", async (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    await heldCreate;
    return r.fulfill({
      status: 201,
      json: { ...MISSION, id: "msn_new", state: "draft", sessions: [] },
    });
  });

  await page.goto("/pulse");
  await ready(page);
  await expect.poll(() => railRowCount(page), { timeout: 10_000 }).toBe(100);

  // A CREATE, held open — its `onCreated` reload will be issued with a callback captured NOW.
  await selectUntracked(page);
  await closeRail(page);
  await page.getByTestId("composer-mode-new").click();
  await page.getByTestId("new-mission-instruction").fill("start something");
  await page.getByTestId("new-mission-project").selectOption("p1");
  await page.getByTestId("new-mission-start").click();

  // …and the operator opens the next page while it is in flight.
  await openMissionRail(page);
  const beforeClick = asked.length;
  await page.locator('[data-testid="rail-load-more"]:visible').first().click();
  // The ask is OUT and its answer is HELD, so nothing about that page has rendered.
  await expect
    .poll(() => asked.length, { timeout: 10_000 })
    .toBe(beforeClick + 1);

  asked.length = 0;
  releaseCreate?.();

  // THE REFRESH RE-READS WHAT THE OPERATOR ASKED FOR, not what had painted when they asked.
  await expect.poll(() => asked, { timeout: 10_000 }).toContain(200);
  releasePage?.();
  await page.waitForTimeout(750);
  await expect.poll(() => railRowCount(page), { timeout: 10_000 }).toBe(200);
});

test("an OLDER refresh cannot narrow the window you just opened", async ({
  page,
}) => {
  // #896 review 26, finding 1 — the OTHER half of the inverse ordering, and the half that does
  // not show up until the refresh AFTER it. Restoring the rows is not enough if the console
  // forgets how wide the rail is: a refresh issued for 100, landing after a LOAD MORE recorded
  // 200, read only its own width and set the window back — so the operator's pages came back and
  // the NEXT mutation collapsed them again, which is the review-25 bug returning one beat later.
  //
  // Red against a window any accepted answer may narrow: the second mutation asks for 100.
  await stub(page);
  const ALL = [
    missionRow({ ...HELD_ROW, state: "running" }),
    // `msn_p*`, so none of them can collide with HELD_ROW's own id — a collision is deduped
    // away and the rail is one row short of the page, which reads exactly like a paging bug.
    ...Array.from({ length: 199 }, (_, i) =>
      missionRow({ id: `msn_p${i}`, title: `M${i}`, state: "running" }),
    ),
  ];
  const asked: { limit: number; offset: number }[] = [];
  let holdNext = false;
  // The held refresh carries a RENAME. Same ids in the same order, so the snapshot is unchanged
  // and it is honestly a fresh read of the same list — and it gives the test a way to see that
  // the answer was applied, which "the count did not change" cannot.
  let renamed = false;
  const rowsNow = () =>
    renamed ? [{ ...ALL[0], title: "RENAMED" }, ...ALL.slice(1)] : ALL;
  let releaseRefresh: (() => void) | null = null;
  const heldRefresh = new Promise<void>((r) => (releaseRefresh = r));
  // TWO objectives, so the second mutation is an honest one rather than a re-waive of something
  // the server has already settled.
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (r.request().method() !== "GET") return r.fallback();
    if (u.pathname === "/api/missions") {
      const offset = Number(u.searchParams.get("offset") ?? 0);
      const limit = Number(u.searchParams.get("limit") ?? 100);
      asked.push({ limit, offset });
      if (holdNext) {
        holdNext = false;
        renamed = true;
        await heldRefresh;
      }
      return r.fulfill({
        json: {
          ...missionList(rowsNow().slice(offset, offset + limit)),
          total: ALL.length,
        },
      });
    }
    if (
      u.pathname.startsWith("/api/missions/") &&
      u.pathname.endsWith("/objectives")
    )
      return r.fulfill({
        json: {
          objectives: [
            {
              mission_id: HELD.id,
              key: "pr_open",
              ord: 0,
              title: "A PR is open",
              probe: null,
              probe_args: null,
              gate: true,
              state: "pending",
              met_at: null,
              observed: null,
              source: "playbook",
            },
            {
              mission_id: HELD.id,
              key: "merged",
              ord: 1,
              title: "Merged",
              probe: null,
              probe_args: null,
              gate: true,
              state: "pending",
              met_at: null,
              observed: null,
              source: "playbook",
            },
          ],
        },
      });
    return r.fulfill({ json: { ...HELD, events: [], events_next_seq: null } });
  });
  await page.route("**/api/missions/*/objectives", async (r) => {
    if (r.request().method() !== "PATCH") return r.fallback();
    return r.fulfill({ json: { objectives: [] } });
  });

  await page.goto("/pulse");
  await ready(page);
  await expect.poll(() => railRowCount(page), { timeout: 10_000 }).toBe(100);
  await goToObjectives(page);
  await expect(page.getByTestId("objective").first()).toBeVisible();

  // A MUTATION, whose rail refresh is held open across the click that must outlive it.
  holdNext = true;
  await page.getByTestId("objective-waive").first().click();
  await expect
    .poll(() => asked.filter((a) => a.offset === 0).length, { timeout: 10_000 })
    .toBe(2);

  // …and the operator opens the next page while it is in flight.
  await openMissionRail(page);
  await page.locator('[data-testid="rail-load-more"]:visible').first().click();
  await expect.poll(() => railRowCount(page), { timeout: 10_000 }).toBe(200);

  // The older, narrower answer lands. It IS applied — the rename proves that, and an
  // authoritative read is allowed to be the rail — but it is an answer about the same list, so
  // the pages the operator opened are still exactly the rest of it and stay where they are.
  releaseRefresh?.();
  const railFirst = async () => {
    await openMissionRail(page);
    const t = await railRows(page).first().innerText();
    await closeRail(page);
    return t;
  };
  await expect.poll(railFirst, { timeout: 10_000 }).toContain("RENAMED");
  await expect.poll(() => railRowCount(page), { timeout: 10_000 }).toBe(200);

  // …AND THE WINDOW IT LEFT BEHIND. The next refresh must still ask for what the operator opened.
  asked.length = 0;
  await goToObjectives(page);
  await page.getByTestId("objective-waive").nth(1).click();
  await expect
    .poll(() => asked.map((a) => a.limit), { timeout: 10_000 })
    .toContain(200);
});

test("an OLDER refresh cannot erase the page you opened while it was in flight", async ({
  page,
}) => {
  // #896 review 26, finding 1 — the INVERSE ordering of the test above, and a different bug with
  // the same symptom. There the create settled after the click; here it settles BEFORE it:
  //
  //   1. a create resolves and issues a refresh for the 100 rows that are open;
  //   2. while that is in flight the operator clicks LOAD MORE — the window becomes 200 and the
  //      offset-100 page goes out;
  //   3. the older, narrower refresh comes back.
  //
  // Two things then went wrong at once. The refresh set the window back to 100, reading only its
  // own width over an intent recorded after it was issued; and the page was thrown away because
  // `appliedGen` had moved, which was a PROXY for "the list you indexed into is gone". The
  // operator's click vanished with nothing on screen to say so, and the next refresh asked for
  // the narrow window again, so it never came back.
  //
  // Both responses are HELD and released in order, so this is the ordering rather than a race.
  //
  // Red against a window an older answer may narrow, and against dropping a page whose digest
  // proves it is a continuation of the rail: the rail stops at 100 rows.
  await stub(page);
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { ...OVERVIEW, cards: [LOOSE_CARD] } }),
  );
  const ALL = Array.from({ length: 200 }, (_, i) =>
    missionRow({ id: `msn_${i}`, title: `M${i}`, state: "running" }),
  );
  let releaseCreate: (() => void) | null = null;
  const heldCreate = new Promise<void>((r) => (releaseCreate = r));
  let releaseRefresh: (() => void) | null = null;
  const heldRefresh = new Promise<void>((r) => (releaseRefresh = r));
  let releasePage: (() => void) | null = null;
  const heldPage = new Promise<void>((r) => (releasePage = r));
  const asked: { limit: number; offset: number }[] = [];
  let fromTheTop = 0;
  await mockMissions(page, {
    missions: async (q: URLSearchParams) => {
      const offset = Number(q.get("offset") ?? 0);
      const limit = Number(q.get("limit") ?? 100);
      asked.push({ limit, offset });
      // The APPEND is held until the refresh it is racing has landed.
      if (offset > 0) await heldPage;
      // …and the create's refresh — the SECOND read from the top, the mount being the first — is
      // held open across the click that must outlive it.
      else if ((fromTheTop += 1) === 2) await heldRefresh;
      return {
        ...missionList(ALL.slice(offset, offset + limit)),
        total: ALL.length,
      };
    },
  });
  await page.route("**/api/missions", async (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    await heldCreate;
    return r.fulfill({
      status: 201,
      json: { ...MISSION, id: "msn_new", state: "draft", sessions: [] },
    });
  });

  await page.goto("/pulse");
  await ready(page);
  await expect.poll(() => railRowCount(page), { timeout: 10_000 }).toBe(100);

  await selectUntracked(page);
  await closeRail(page);
  await page.getByTestId("composer-mode-new").click();
  await page.getByTestId("new-mission-instruction").fill("start something");
  await page.getByTestId("new-mission-project").selectOption("p1");
  await page.getByTestId("new-mission-start").click();

  // THE REFRESH GOES FIRST, and is held.
  releaseCreate?.();
  await expect
    .poll(() => asked.filter((a) => a.offset === 0).length, { timeout: 10_000 })
    .toBe(2);

  // …and the operator opens the next page while it is still in flight.
  await openMissionRail(page);
  await page.locator('[data-testid="rail-load-more"]:visible').first().click();
  await expect
    .poll(() => asked.some((a) => a.offset === 100), { timeout: 10_000 })
    .toBe(true);

  // The older, narrower answer lands first…
  releaseRefresh?.();
  await page.waitForTimeout(500);
  // …and then the page it was racing, which is a continuation of the very list it just installed.
  releasePage?.();
  await expect.poll(() => railRowCount(page), { timeout: 10_000 }).toBe(200);
});

test("an OLDER refresh cannot resurrect a row after a NEWER one failed", async ({
  page,
}) => {
  // #896 review 23 finding 2, requested again in review 25. `appliedGen` only advances on SUCCESS,
  // so a refresh that FAILED left it where it was — and an older authoritative response arriving
  // afterwards compared equal and was ACCEPTED, reinstating a list from before whatever had
  // happened since, with no later poll to repair it. The failure path already fenced on the newest
  // ISSUED generation; the success path was the half that did not.
  //
  // The held request is the ARCHIVE's own refresh, not the mount: a response held across the
  // console's first render is never applied even against the unfixed build, so a test built on
  // that ordering passes for a reason unrelated to the fence.
  //
  // Red against a success fenced on `appliedGen`: Alpha comes back.
  await stub(page);
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { ...OVERVIEW, cards: [LOOSE_CARD] } }),
  );
  const ALPHA = missionRow({ id: "msn_a", title: "Alpha", state: "done" });
  const BRAVO = missionRow({ id: "msn_b", title: "Bravo", state: "running" });
  let releaseStale: (() => void) | null = null;
  const heldStale = new Promise<void>((r) => (releaseStale = r));
  let heldSeen = false;
  // PHASES, not a request count: the console reads the list more than once while settling, and a
  // counted fixture starts failing under the operator's feet.
  let phase: "ok" | "hold" | "fail" = "ok";
  await mockMissions(page, {
    missions: missionList([ALPHA, BRAVO]),
    mission: { ...MISSION, state: "done", events: [], events_next_seq: null },
  });
  // THE LIST ONLY, registered after `mockMissions` so it wins for that exact path and the detail
  // reads keep the shapes their components expect. A REJECTED read is the whole point: a 200 with
  // an empty list is a SUCCESS and advances the applied generation, so a fixture built on one
  // cannot tell the two fences apart.
  await page.route("**/api/missions**", async (r) => {
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions" || r.request().method() !== "GET")
      return r.fallback();
    if (phase === "hold") {
      phase = "fail";
      heldSeen = true;
      await heldStale;
    } else if (phase === "fail") {
      return r.fulfill({
        status: 500,
        json: { detail: "the store could not be read" },
      });
    }
    return r.fulfill({ json: missionList([ALPHA, BRAVO]) });
  });
  await page.route("**/api/missions/*/archive", (r) =>
    r.fulfill({ json: { ...MISSION, id: "msn_a", archived_at: 1 } }),
  );

  await page.goto("/pulse");
  await selectMission(page, "Alpha");
  phase = "hold";
  await page.getByTestId("mission-archive").click();
  await page.getByTestId("mission-archive").click();
  await expect.poll(() => heldSeen, { timeout: 10_000 }).toBe(true);

  // A NEWER authoritative read, refused — `appliedGen` is untouched by it, which is the defect.
  await selectUntracked(page);
  await closeRail(page);
  await page.getByTestId("composer-mode-new").click();
  await page.getByTestId("new-mission-instruction").fill("start something");
  await page.getByTestId("new-mission-project").selectOption("p1");
  await page.getByTestId("new-mission-start").click();
  await expect.poll(() => phase, { timeout: 10_000 }).toBe("fail");

  releaseStale?.();
  await page.waitForTimeout(750);

  await openMissionRail(page);
  expect((await railRows(page).allInnerTexts()).join(" ")).not.toContain(
    "Alpha",
  );
});
