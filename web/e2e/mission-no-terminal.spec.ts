/** No terminal required — VIEW SCREEN and relay, from the thread (#894, Phase 6 of #840).
 *
 * #840's central promise: *"Jumping into the terminal stays available at every moment; it stops
 * being required."* Seeing what an agent is showing, and answering it, are the two most ordinary
 * reasons to go and open one.
 *
 * The property a browser is needed for is the one about SIDE EFFECTS: looking must not attach.
 * A jsdom test can prove a fetch was made; only a real page can show that nothing else was —
 * no socket, no lease, no resize frame.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
} from "./mission-console";

const T = 1_700_000_000;
const KEY = "claude:11111111-1111-1111-1111-111111111111";

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

async function stub(
  page: Page,
  over: {
    mission?: Record<string, unknown>;
    sessions?: { session_key: string; role?: string; removed_at: null }[];
  } = {},
) {
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
    r.fulfill({ json: { projects: [] } }),
  );
  await mockMissions(page, {
    missions: missionList([
      missionRow({ id: "msn_1", title: "Ship it", session_keys: [KEY] }),
    ]),
    mission: {
      ...MISSION,
      id: "msn_1",
      title: "Ship it",
      sessions: [{ session_key: KEY, removed_at: null }],
      events: [],
      events_next_seq: null,
      ...over.mission,
    },
    context: {
      id: "msn_1",
      project_id: "",
      cwd: "/repo",
      sessions: over.sessions ?? [{ session_key: KEY, removed_at: null }],
      git: null,
      git_error: null,
    },
  });
}

async function openContext(page: Page) {
  await expect(page.getByTestId("mission-console")).toBeVisible();
  const opener = page.getByTestId("rail-drawer-open");
  if (await opener.isVisible().catch(() => false)) await opener.click();
  await page.locator('[data-testid="rail-mission"]:visible').first().click();
  const drawer = page.getByTestId("rail-drawer");
  if (await drawer.isVisible().catch(() => false))
    await page.keyboard.press("Escape");
  // The context panel is a stop on a phone and a column on a desktop.
  const stop = page.getByTestId("stop-objectives");
  if (await stop.isVisible().catch(() => false)) await stop.click();
}

test("VIEW SCREEN reads the live screen and OPENS NO SOCKET", async ({
  page,
}) => {
  // The property that makes peeking safe: attaching marks the viewer busy and suppresses
  // autonomous delivery, so "let me just look" would silently pause the orchestrator on the
  // very mission being checked on.
  const sockets: string[] = [];
  await page.addInitScript(() => {
    const real = window.WebSocket;
    // @ts-expect-error - test shim
    window.WebSocket = function (url: string, ...rest: unknown[]) {
      (window as unknown as { __sockets: string[] }).__sockets ??= [];
      (window as unknown as { __sockets: string[] }).__sockets.push(
        String(url),
      );
      // @ts-expect-error - test shim
      return new real(url, ...rest);
    };
  });
  await stub(page);
  const asked: string[] = [];
  // THE MISSION-SCOPED ROUTE, registered after `mockMissions`'s catch-all so Playwright's
  // most-recently-registered-wins matching reaches it (#903 review 3, finding 1).
  await page.route("**/api/missions/*/screen/**", (r) => {
    asked.push(r.request().url());
    return r.fulfill({
      json: { kind: "screen", text: "$ pytest\n12 passed", available: true },
    });
  });

  await page.goto("/pulse");
  await openContext(page);
  await page.locator('[data-testid="view-screen"]:visible').first().click();

  await expect(
    page.locator('[data-testid="screen-text"]:visible').first(),
  ).toContainText("12 passed");
  expect(asked.length).toBe(1);
  // The MISSION is in the request, which is what makes membership checkable at request time.
  expect(asked[0]).toContain("/api/missions/msn_1/screen/");
  // …and nothing attached.
  sockets.push(
    ...(await page.evaluate(
      () => (window as unknown as { __sockets?: string[] }).__sockets ?? [],
    )),
  );
  expect(sockets.filter((u) => u.includes("/ws/term"))).toEqual([]);
});

test("an EMPTY screen says so rather than looking broken", async ({ page }) => {
  await stub(page);
  await page.route("**/api/missions/*/screen/**", (r) =>
    r.fulfill({ json: { kind: "screen", text: "", available: false } }),
  );
  await page.goto("/pulse");
  await openContext(page);
  await page.locator('[data-testid="view-screen"]:visible').first().click();
  await expect(
    page.locator('[data-testid="screen-unavailable"]:visible').first(),
  ).toContainText("no output yet");
});

test("RELAY posts the operator's own text to the mission's session", async ({
  page,
}) => {
  await stub(page);
  const posts: unknown[] = [];
  await page.route("**/api/missions/*/relay", (r) => {
    posts.push(r.request().postDataJSON());
    return r.fulfill({
      json: { action_id: "a1", state: "delivered", session_key: KEY },
    });
  });
  await page.goto("/pulse");
  await openContext(page);
  await page
    .locator('[data-testid="relay-input"]:visible')
    .first()
    .fill("yes, go ahead");
  await page.locator('[data-testid="relay-send"]:visible').first().click();

  await expect.poll(() => posts.length).toBe(1);
  const body = posts[0] as Record<string, unknown>;
  expect(body.session_key).toBe(KEY);
  expect(body.text).toBe("yes, go ahead");
  await expect(
    page.locator('[data-testid="relay-note"]:visible').first(),
  ).toContainText("Sent.");
});

test("a REFUSED relay says which refusal it was", async ({ page }) => {
  // "delivered" is the only state that means the bytes landed. Flattening `stale` to "sent" is
  // the lie the whole three-way outcome exists to prevent — a viewer at the keyboard, a session
  // that is not live and a mission being archived are different problems with different fixes.
  await stub(page);
  await page.route("**/api/missions/*/relay", (r) =>
    r.fulfill({
      json: {
        action_id: "a1",
        state: "stale",
        detail: "a viewer is attached",
        session_key: KEY,
      },
    }),
  );
  await page.goto("/pulse");
  await openContext(page);
  await page
    .locator('[data-testid="relay-input"]:visible')
    .first()
    .fill("are you there?");
  await page.locator('[data-testid="relay-send"]:visible').first().click();
  await expect(
    page.locator('[data-testid="relay-note"]:visible').first(),
  ).toContainText("a viewer is attached");
  // …AND THE DRAFT SURVIVES IT (#903 review 2, finding 1). A refusal sends zero bytes and is
  // precisely the case the operator retries, so clearing the box deletes an instruction they may
  // have spent a minute writing. Only a delivery empties it.
  await expect(
    page.locator('[data-testid="relay-input"]:visible').first(),
  ).toHaveValue("are you there?");
});

test("a DELIVERED relay is the only outcome that empties the box", async ({
  page,
}) => {
  // The mirror. Keeping the text after a successful send would invite the operator to send the
  // same instruction twice.
  await stub(page);
  await page.route("**/api/missions/*/relay", (r) =>
    r.fulfill({
      json: {
        action_id: "a1",
        state: "delivered",
        detail: "",
        session_key: KEY,
      },
    }),
  );
  await page.goto("/pulse");
  await openContext(page);
  await page
    .locator('[data-testid="relay-input"]:visible')
    .first()
    .fill("carry on");
  await page.locator('[data-testid="relay-send"]:visible').first().click();
  await expect(
    page.locator('[data-testid="relay-note"]:visible').first(),
  ).toContainText("Sent.");
  await expect(
    page.locator('[data-testid="relay-input"]:visible').first(),
  ).toHaveValue("");
});

test("with TWO sessions each block names its target, and SEND goes to that one", async ({
  page,
}) => {
  // The blocks are otherwise identical, and what is on the other end of each is a live agent
  // with permission bypass. An operator who cannot tell which SEND box belongs to which session
  // can relay an instruction into the wrong one (#903 review 2, finding 2).
  const B = "claude:22222222-2222-2222-2222-222222222222";
  await stub(page);
  const two = [
    { session_key: KEY, removed_at: null, role: "primary" },
    { session_key: B, removed_at: null, role: "helper" },
  ];
  await page.route(/\/api\/missions\/msn_1\/context$/, (r) =>
    r.fulfill({
      json: {
        id: "msn_1",
        project_id: "",
        cwd: "/repo",
        sessions: two,
        git: null,
        git_error: null,
      },
    }),
  );
  const sent: unknown[] = [];
  await page.route("**/api/missions/*/relay", (r) => {
    sent.push(r.request().postDataJSON());
    return r.fulfill({
      json: { action_id: "a1", state: "delivered", detail: "", session_key: B },
    });
  });

  await page.goto("/pulse");
  await openContext(page);

  const blocks = page.locator('[data-testid="mission-screen"]:visible');
  await expect(blocks).toHaveCount(2);
  // Each block SAYS which session it is…
  await expect(blocks.nth(0).getByTestId("screen-target")).toContainText(KEY);
  await expect(blocks.nth(1).getByTestId("screen-target")).toContainText(B);
  // …and carries it in the accessible name, so the keyboard path is not a second unlabelled one.
  await expect(blocks.nth(1).getByTestId("relay-send")).toHaveAttribute(
    "aria-label",
    `Send to ${B}`,
  );

  // …and typing into the SECOND block sends to the SECOND session.
  await blocks.nth(1).getByTestId("relay-input").fill("you, not the other one");
  await blocks.nth(1).getByTestId("relay-send").click();
  await expect.poll(() => sent.length).toBe(1);
  expect((sent[0] as Record<string, unknown>).session_key).toBe(B);
});

test("every no-terminal control clears 44px", async ({ page }) => {
  await stub(page);
  await page.goto("/pulse");
  await openContext(page);
  for (const id of ["view-screen", "relay-input", "relay-send"]) {
    const box = await page
      .locator(`[data-testid="${id}"]:visible`)
      .first()
      .boundingBox();
    expect(box, id).not.toBeNull();
    expect(box!.height, id).toBeGreaterThanOrEqual(44);
  }
});

test("a session DETACHED and re-adopted elsewhere stops answering under this mission", async ({
  page,
}) => {
  // #903 review 3, finding 1. Membership is a row another tab can change, and the context is
  // loaded ONCE — so an already-open mission A kept its screen block and went on asking for the
  // same key after the session had been detached and adopted into B. The mission-agnostic
  // evidence endpoint answered, and B's live output rendered under A's heading. Nothing on the
  // page was wrong except whose work the operator was reading.
  //
  // Red against a client that reads `/api/pulse/evidence/{key}`: that route has no mission in it
  // and cannot refuse.
  await stub(page);
  let held = true;
  const asked: string[] = [];
  await page.route("**/api/missions/*/screen/**", (r) => {
    asked.push(r.request().url());
    if (!held)
      return r.fulfill({
        status: 409,
        json: { detail: "this mission no longer holds that session" },
      });
    return r.fulfill({
      json: { kind: "screen", text: "$ pytest\n12 passed", available: true },
    });
  });
  // The roster the console re-reads after the refusal — by then the session is gone from it.
  await page.route("**/api/missions/*/context", (r) =>
    r.fulfill({
      json: {
        id: "msn_1",
        project_id: "",
        cwd: "/repo",
        sessions: held ? [{ session_key: KEY, removed_at: null }] : [],
        git: null,
        git_error: null,
      },
    }),
  );

  await page.goto("/pulse");
  await openContext(page);
  await page.locator('[data-testid="view-screen"]:visible').first().click();
  await expect(
    page.locator('[data-testid="screen-text"]:visible').first(),
  ).toContainText("12 passed");

  // ANOTHER TAB detaches it and mission B adopts it.
  held = false;
  await page.locator('[data-testid="view-screen"]:visible').first().click();

  // 1. NO OUTPUT is rendered — not the new owner's, and not the stale copy of the old one.
  await expect(page.locator('[data-testid="screen-text"]')).toHaveCount(0);
  // 2. …and the block itself goes, because the roster is re-read on the refusal. Leaving it
  //    would leave a SEND box pointing at an agent that is now somebody else's.
  await expect(page.locator('[data-testid="mission-screen"]')).toHaveCount(0);
});

test("a REFRESH that fails clears the screen rather than freezing it", async ({
  page,
}) => {
  // #903 review 3, finding 6. This component's own contract is that a frozen screen is worse
  // than none: what it shows is labelled "what this agent is doing right now", and a failed
  // refresh that left the last successful read on screen kept making that claim about output of
  // unknown age — beside an error saying the read had failed.
  await stub(page);
  let ok = true;
  await page.route("**/api/missions/*/screen/**", (r) =>
    ok
      ? r.fulfill({
          json: {
            kind: "screen",
            text: "$ pytest\n12 passed",
            available: true,
          },
        })
      : r.fulfill({ status: 500, json: { detail: "boom" } }),
  );

  await page.goto("/pulse");
  await openContext(page);
  await page.locator('[data-testid="view-screen"]:visible').first().click();
  await expect(
    page.locator('[data-testid="screen-text"]:visible').first(),
  ).toContainText("12 passed");

  ok = false;
  await page.locator('[data-testid="view-screen"]:visible').first().click();

  await expect(
    page.locator('[data-testid="screen-error"]:visible').first(),
  ).toBeVisible();
  // THE OLD OUTPUT IS GONE. It is the assertion the reviewed shape fails.
  await expect(page.locator('[data-testid="screen-text"]')).toHaveCount(0);
});

test("a relay's OUTCOME survives a reload, and the three answers stay different", async ({
  page,
}) => {
  // #903 review 3, finding 5. The route settles what happened into the event's own `meta`, and
  // a timeline row that showed only the text made a refused, ambiguous or in-flight relay look
  // exactly like a delivered one — the operator's words on screen with nothing to say they never
  // arrived. After a reload that record is the only one there is.
  await stub(page);
  const relayEvent = (seq: number, state: string, detail: string) => ({
    seq,
    at: T - seq,
    kind: "operator_msg",
    text: `message ${seq}`,
    session_key: KEY,
    action_id: `relay_${seq}`,
    meta: { relay: true, state, ...(detail ? { detail } : {}) },
  });
  await page.route(/\/api\/missions\/msn_1(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        ...MISSION,
        id: "msn_1",
        title: "Ship it",
        sessions: [{ session_key: KEY, removed_at: null }],
        events: [
          relayEvent(4, "delivered", ""),
          relayEvent(3, "indeterminate", "OSError"),
          relayEvent(2, "failed", "the session is not live"),
          relayEvent(1, "sending", ""),
        ],
        events_next_seq: null,
      },
    }),
  );

  await page.goto("/pulse");
  await expect(page.getByTestId("mission-console")).toBeVisible();
  const opener = page.getByTestId("rail-drawer-open");
  if (await opener.isVisible().catch(() => false)) await opener.click();
  await page.locator('[data-testid="rail-mission"]:visible').first().click();
  const drawer = page.getByTestId("rail-drawer");
  if (await drawer.isVisible().catch(() => false))
    await page.keyboard.press("Escape");
  const stop = page.getByTestId("stop-timeline");
  if (await stop.isVisible().catch(() => false)) await stop.click();

  const states = page.locator('[data-testid="timeline-relay-state"]:visible');
  await expect(states).toHaveCount(4);
  // FOUR DIFFERENT ANSWERS, and the ambiguous one is not dressed up as a failure — inviting a
  // second copy of an instruction the agent may already have is the harm.
  await expect(states.nth(0)).toHaveText("delivered");
  await expect(states.nth(1)).toContainText("may not have arrived");
  await expect(states.nth(1)).toHaveAttribute("data-relay-kind", "warn");
  await expect(states.nth(2)).toContainText("the session is not live");
  await expect(states.nth(2)).toHaveAttribute("data-relay-kind", "bad");
  await expect(states.nth(3)).toHaveText("sending…");
});

test("an AMBIGUOUS delivery is not reported as 'Not sent'", async ({
  page,
}) => {
  // #903 review 4, finding 3. Once `deliver` has claimed the action the bytes may already be in
  // the pty, and the server says exactly that. The client prefixed every rejected mutation with
  // "Not sent —", which about THIS outcome invites a retry of an instruction the agent might
  // already have — the one thing this whole path is careful about.
  //
  // Red against a catch that treats every 5xx as a refusal.
  await stub(page);
  await page.route("**/api/missions/*/relay", (r) =>
    r.fulfill({
      status: 502,
      json: {
        detail:
          "the relay may or may not have landed (OSError); check the session before sending it again",
        state: "indeterminate",
        action_id: "a1",
      },
    }),
  );
  await page.goto("/pulse");
  await openContext(page);
  await page
    .locator('[data-testid="relay-input"]:visible')
    .first()
    .fill("restart the build");
  await page.locator('[data-testid="relay-send"]:visible').first().click();

  const note = page.locator('[data-testid="relay-note"]:visible').first();
  await expect(note).toContainText("uncertain");
  await expect(note).not.toContainText("Not sent");
  // …and the DRAFT SURVIVES, because the operator decides after they have looked.
  await expect(
    page.locator('[data-testid="relay-input"]:visible').first(),
  ).toHaveValue("restart the build");
});

// ---- the bounded, approval-gated sub-agent spawn (#894) -----------------------------
//
// The server is the only thing that ENFORCES the cap — `claim_spawn` counts and reserves in one
// transaction — so these are about what the console OFFERS and what it says. That is not the
// lesser half: a control that can only produce a 409 teaches the operator to distrust the ones
// that work, and a refusal that eats their draft teaches them not to type.

const SUB_A = "claude:9b02bbbb-2222-2222-2222-222222222222";
const SUB_B = "claude:cccccccc-3333-3333-3333-333333333333";

/** A running mission with an engine and a cap, plus whatever roster the test needs. */
// THE PRODUCER'S SHAPE AFTER A REAL DISPATCH, not a convenient one (review 1, finding 3).
//
// This fixture used to carry `engine: "claude"`, and the console gated the spawn control on
// `mission.engine || mission.plan.engine`. Both are EMPTY after an ordinary create -> plan ->
// dispatch: the create route stores no engine, `claim_plan` moves it to the dispatch row and
// deletes the plan, and a successful settlement deletes the dispatch row. So every one of these
// tests passed against a DTO the server never sends, and the control was absent in the one flow
// it exists for. The injected engine was the whole reason that went unnoticed.
//
// `engine` and `plan` are pinned to null here deliberately, so a regression to the old gate fails
// this file rather than passing it.
const RUNNING = {
  state: "running",
  engine: null,
  plan: null,
  spawn_cap: 2,
  spawn_live: 0,
  spawn_engine: "claude",
  spawn_cwd: "/repo/acme",
};

test("SUB-AGENT is offered beside the session it would work alongside", async ({
  page,
}) => {
  // The control names its target. A spawn parented to "the mission" rather than to a session
  // leaves a tree that cannot be read afterwards — `spawned_by` is where "whose sub-agent is
  // this" is answered — so the button lives in the roster block, next to the key it will name.
  await stub(page, { mission: RUNNING });
  await page.goto("/pulse");
  await openContext(page);
  const row = page.getByTestId("roster-session").first();
  await expect(row.getByTestId("spawn-open")).toBeEnabled();
});

test("AT CAP the control is withheld, and says the limit is not a permission", async ({
  page,
}) => {
  // Two claims, and the second is the one a review would catch. The button is disabled, because a
  // tap that could only 409 should not be offered. And the reason distinguishes a RESOURCE GUARD
  // from a permission — reading a fan-out bound as a safety boundary is the mistake the operator
  // is most likely to make, and the copy is where that gets settled.
  // THE COUNT COMES FROM THE SERVER, on the server's own definition (review 1, finding 5). This
  // used to be expressed by putting two `role: "sub"` rows in the roster and letting the console
  // count them, which is exactly the second definition that disagreed with the claim by one. The
  // roster rows stay — the panel still names its parent — but the budget is `spawn_live`.
  await stub(page, {
    mission: { ...RUNNING, spawn_live: 2 },
    sessions: [
      { session_key: KEY, role: "primary", removed_at: null },
      { session_key: SUB_A, role: "sub", removed_at: null },
      { session_key: SUB_B, role: "sub", removed_at: null },
    ],
  });
  await page.goto("/pulse");
  await openContext(page);
  const open = page.getByTestId("spawn-open").first();
  await expect(open).toBeDisabled();
  await expect(open).toHaveText(/2\/2/);
  await expect(open).toHaveAttribute("title", /not a permission/);
});

test("the panel SHOWS the directory and asserts it back on START", async ({
  page,
}) => {
  // An approval the operator could not read is not an approval (review 1, finding 1). The server
  // resolving the path stops a client naming one; it does not stop the path moving under a panel
  // somebody is already reading. So the consequence line names the directory, and the same value
  // rides back as `expect_cwd` for the server to compare and discard.
  const sent: Array<Record<string, unknown>> = [];
  await stub(page, { mission: RUNNING });
  await page.route("**/api/missions/*/spawn", (r) => {
    sent.push(r.request().postDataJSON());
    return r.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ state: "running", reason: "", session_key: null }),
    });
  });
  await page.goto("/pulse");
  await openContext(page);
  await page.getByTestId("spawn-open").first().click();
  // It is on screen, not merely in the payload.
  await expect(page.getByTestId("spawn-cwd")).toHaveText("/repo/acme");
  await page.getByTestId("spawn-brief").fill("read the diff");
  await page.getByTestId("spawn-start").click();
  await expect.poll(() => sent.length).toBeGreaterThan(0);
  expect(sent[0].expect_cwd).toBe("/repo/acme");
});

test("a POLL that lands mid-spawn does not eat the draft", async ({ page }) => {
  // The refusal test below keeps its GET fixture `running` throughout, so the transition never
  // happens and the bug hides behind it. A real claim moves the mission through `dispatching` for
  // the length of the launch; when the periodic detail poll observed that, a `running`-only gate
  // unmounted the whole panel and took the typed brief with it. The refusal then restored
  // `running` and remounted an empty editor — the operator's words gone, with an error beside it.
  let dispatching = false;
  await stub(page, { mission: RUNNING });
  // Registered AFTER stub, so it wins: Playwright matches most-recent-first.
  await page.route("**/api/missions/*", (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    return r.fulfill({
      json: {
        ...MISSION,
        id: "msn_1",
        title: "Ship it",
        sessions: [{ session_key: KEY, removed_at: null }],
        events: [],
        events_next_seq: null,
        ...RUNNING,
        state: dispatching ? "dispatching" : "running",
      },
    });
  });
  let release: (() => void) | null = null;
  const held = new Promise<void>((res) => (release = res));
  await page.route("**/api/missions/*/spawn", async (r) => {
    dispatching = true; // the claim landed; the mission is now in transit
    await held;
    return r.fulfill({
      status: 409,
      json: { detail: "autonomy is off — this instance watches and proposes" },
    });
  });

  await page.goto("/pulse");
  await openContext(page);
  await page.getByTestId("spawn-open").first().click();
  await page.getByTestId("spawn-brief").fill("Review the open PR");
  await page.getByTestId("spawn-start").click();

  // While the launch is in flight the panel must still be there, brief intact.
  await expect(page.getByTestId("spawn-card")).toBeVisible();
  await expect(page.getByTestId("spawn-brief")).toHaveValue("Review the open PR");

  release?.();
  await expect(page.getByTestId("spawn-error")).toContainText(/autonomy is off/);
  await expect(page.getByTestId("spawn-brief")).toHaveValue(
    "Review the open PR",
    { timeout: 5000 },
  );
});

test("a FAILED start is reported, even though the mission stays running", async ({
  page,
}) => {
  // Review 3, finding 2. A child's failure deliberately no longer fails its parent, so the spawn
  // response now carries `state: "running"` for a launch that did not start. The console read
  // exactly that as success — card closed, brief cleared, reason never shown — and told the
  // operator an agent was working when none had started. `outcome` is the attempt's own verdict.
  await stub(page, { mission: RUNNING });
  await page.route("**/api/missions/*/spawn", (r) =>
    r.fulfill({
      status: 200,
      json: {
        state: "running", // the MISSION is fine…
        outcome: "failed", // …the ATTEMPT is not
        reason: "the session never registered within 90s",
        session_key: "claude:aaaa",
      },
    }),
  );
  await page.goto("/pulse");
  await openContext(page);
  await page.getByTestId("spawn-open").first().click();
  await page.getByTestId("spawn-brief").fill("Review the open PR");
  await page.getByTestId("spawn-start").click();

  // The reason reaches the operator…
  await expect(page.getByTestId("console-note")).toContainText(/never registered/);
  // …and the work they typed is still there, because nothing started.
  await expect(page.getByTestId("spawn-brief")).toHaveValue("Review the open PR");
});

test("a REFUSED spawn keeps the brief that was typed", async ({ page }) => {
  // The refusals here are ordinary — autonomy switched off, a host that cannot contain an agent —
  // and they arrive after the operator has written the work down. Clearing the box on failure
  // makes them retype it, which is how a control teaches people not to use it.
  await stub(page, { mission: RUNNING });
  await page.route("**/api/missions/*/spawn", (r) =>
    r.fulfill({
      status: 409,
      json: { detail: "autonomy is off — this instance watches and proposes" },
    }),
  );
  await page.goto("/pulse");
  await openContext(page);
  await page.getByTestId("spawn-open").first().click();
  await page.getByTestId("spawn-brief").fill("Review the open PR");
  await page.getByTestId("spawn-start").click();
  await expect(page.getByTestId("spawn-error")).toContainText(
    /autonomy is off/,
  );
  await expect(page.getByTestId("spawn-brief")).toHaveValue(
    "Review the open PR",
  );
});

test("a spawn that STARTED BUT WAS NOT CONFIRMED is reported, not silently dropped", async ({
  page,
}) => {
  // `alive is not started` reaching the operator. The launcher produced a process, the engine's
  // store had no record of it, so the mission did not adopt it and it was torn down. The route
  // answers 200 with a non-running state and a reason — and the reason is the useful half.
  // Swallowing it would leave the operator believing a sub-agent is working.
  await stub(page, { mission: RUNNING });
  await page.route("**/api/missions/*/spawn", (r) =>
    r.fulfill({
      json: {
        state: "failed",
        reason: "the engine's store has no such session",
        session_key: null,
      },
    }),
  );
  await page.goto("/pulse");
  await openContext(page);
  await page.getByTestId("spawn-open").first().click();
  await page.getByTestId("spawn-brief").fill("Review the open PR");
  await page.getByTestId("spawn-start").click();
  await expect(page.getByTestId("console-note")).toContainText(
    /store has no such session/,
  );
});

test("the consequence is ANNOUNCED and tied to the button that acts on it", async ({
  page,
}) => {
  // The sentence says an unattended agent starts. A screen reader reaching a button labelled only
  // "START SUB-AGENT" would never hear it, so it is a live region AND the button's description —
  // the same gap the plan card's dispatch confirmation had.
  await stub(page, { mission: RUNNING });
  await page.goto("/pulse");
  await openContext(page);
  await page.getByTestId("spawn-open").first().click();
  const consequence = page.getByTestId("spawn-consequence");
  await expect(consequence).toContainText(/unattended/);
  await expect(consequence).toHaveAttribute("role", "status");
  const id = await consequence.getAttribute("id");
  await expect(page.getByTestId("spawn-start")).toHaveAttribute(
    "aria-describedby",
    String(id),
  );
});
