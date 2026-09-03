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
    },
    context: {
      id: "msn_1",
      project_id: "",
      cwd: "/repo",
      sessions: [{ session_key: KEY, removed_at: null }],
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
