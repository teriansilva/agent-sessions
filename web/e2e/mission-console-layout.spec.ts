/** #929 — the Pulse shell is gone, and the two defects its removal exposed are closed.
 *
 *  These are real-browser tests because both findings are LAYOUT facts a DOM emulator cannot
 *  answer: one is a media query, the other is what an operator can reach at a given width.
 *
 *  The invariant under test is reachability of CONTENT, never the presence of a tab. A tab that
 *  renders nothing would satisfy a `toBeVisible()` on the control and still leave the operator
 *  exactly where they started.
 */
import { expect, test, type Page } from "@playwright/test";

import { missionList, missionRow, mockMissions } from "./mission-console";

const T = 1_700_000_000;

/** A typed deferred, because the obvious spelling does not compile.
 *
 *  `let release: (() => void) | null = null` assigned inside a Promise executor is narrowed by
 *  control-flow analysis to `null` at every later use — TS cannot see that the executor ran — so
 *  `release?.()` has type `never` and reports TS2349 "not callable". The production build misses
 *  it (its config includes `src` and excludes tests) and Playwright transpiles without checking,
 *  so this only surfaces under a typecheck aimed at the specs themselves. A definite-assignment
 *  assertion states the fact the executor guarantees instead of guessing around it. */
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
  pulse: { configured: true },
};
/** One untracked session, so the UNTRACKED view actually renders. With zero sessions AND zero
 *  missions the console selects nothing and neither branch mounts — which would make every
 *  assertion below vacuous rather than failing. */
const CARD = {
  id: "claude:11111111-1111-1111-1111-111111111111",
  engine: "claude",
  title: "a live session",
  cwd: "/home/u/agent-sessions",
  project: {
    kind: "project",
    id: "p1",
    name: "agent-sessions",
    color: "#ffb000",
  },
  last_activity: T - 120,
  ai_summary: "landed the parser",
  intervention_required: false,
  intervention_reason: "",
  reviewed_at: T - 120,
  live: true,
  state: "in_flight",
  synthesis: null,
};

const OVERVIEW = {
  cache_version: 1,
  generated_at: T - 60,
  window_days: 3,
  scan_depth: "medium",
  input_fingerprint: "fp",
  synthesis_skipped: false,
  banner: null,
  cards: [CARD],
};

/** `missions: []` is a SUCCESSFUL read that found nothing — the first-run case. */
async function stub(
  page: Page,
  opts: {
    missions?: unknown[];
    storeError?: string;
    cards?: unknown[];
    // The resolver form of `mockMissions`, awaited per request — the supported way to hold a
    // list open. Re-registering the missions glob from a test instead outranks the helper's
    // per-mission handlers (Playwright matches most-recently-registered first) and serves the
    // list shape for every `/api/missions/{id}` read.
    missionsResolver?: (q: URLSearchParams) => Promise<unknown>;
  } = {},
) {
  const rows = opts.missions ?? [];
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
  // The overview is `/api/pulse` exactly — `**/api/pulse/overview` matches nothing and leaves
  // the page with no cards, hence no untracked view and nothing to assert against.
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { ...OVERVIEW, cards: opts.cards ?? [CARD] } }),
  );
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
  // `missionList(...)` — NOT a bare array. `mockMissions` does `m ?? EMPTY_MISSIONS`, and `[]`
  // is not nullish, so passing one fulfils with an ARRAY where the console expects
  // `{missions, total, …}`; the parse fails, no list is applied, and `listLoaded` never flips.
  // That silently defeats the first-run assertion rather than failing it honestly.
  await mockMissions(page, {
    missions:
      opts.missionsResolver ?? missionList(rows, opts.storeError ?? null),
  });
}

test.describe("stops are reachable at every width (#929)", () => {
  for (const width of [1399, 1400, 1600]) {
    test(`no mission selected at ${width}px: OBJECTIVES and TIMELINE render content`, async ({
      page,
    }) => {
      // The old rule hid the stop strip at >=1400px on the theory that a detail column always
      // replaced it. With no mission there IS no column, so at 1400+ the operator saw neither —
      // and at 1399 they saw both. This asserts the width does not decide it.
      await page.setViewportSize({ width, height: 900 });
      await stub(page);
      await page.goto("/pulse");
      await page.getByTestId("pane").waitFor();

      await page.getByTestId("stop-objectives").click();
      await expect(page.getByTestId("no-mission-objectives")).toContainText(
        /objectives belong to a mission/i,
      );

      await page.getByTestId("stop-timeline").click();
      await expect(page.getByTestId("no-mission-timeline")).toContainText(
        /timeline belongs to a mission/i,
      );
    });
  }

  test("with a mission selected at 1600px the strip is STILL there (#942)", async ({
    page,
  }) => {
    // THIS ASSERTION IS THE INVERSE OF WHAT IT WAS, and the inversion is the point. #929 fixed
    // "the tabs vanish at ≥1400px" by keying the hide on whether a detail column had actually
    // rendered — the honest fix for the rule as it stood. #942 deleted the column, so there is
    // nothing to replace the tabs with and nothing to key off: they are simply always there.
    await page.setViewportSize({ width: 1600, height: 900 });
    await stub(page, { missions: [missionRow()] });
    await page.goto("/pulse");
    // Selected EXPLICITLY. With untracked sessions present the console auto-selects UNTRACKED,
    // so asserting on the default view would have tested the other branch entirely.
    await page
      .getByRole("button", { name: /Kimi transcript adapter/i })
      .first()
      .click();
    await expect(page.getByTestId("mission-state")).toBeVisible();
    await expect(page.getByTestId("detail-column")).toHaveCount(0);
    await expect(page.getByTestId("stop-objectives")).toBeVisible();
    await expect(page.getByTestId("stop-context")).toBeVisible();
  });
});

test("a fresh install leads with the composer, not with disabled ADOPTs (#929)", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await stub(page);
  await page.goto("/pulse");

  const firstRun = page.getByTestId("first-run");
  await expect(firstRun).toContainText(/start your first mission/i);

  // ASSERTED AS GEOMETRY, NOT AS DOM NESTING (#930 review 1, finding 1). The composer used to
  // be a CHILD of the first-run block, which is what made the fresh-install flip a remount and
  // cost the operator their draft. It is now one fixed mount whose ORDER moves, so "leads with
  // the composer" has to be asked of the layout: is it above the session list, and below the
  // invitation that explains it. Containment would now pass for a composer that renders
  // nowhere near either.
  const composer = page.getByTestId("composer-mode-new");
  await expect(composer).toBeVisible();
  const sessions = page.getByTestId("untracked-session").first();
  await expect(sessions).toBeVisible();

  const [fb, cb, sb] = [
    (await firstRun.boundingBox())!,
    (await composer.boundingBox())!,
    (await sessions.boundingBox())!,
  ];
  expect(cb.y).toBeGreaterThan(fb.y);
  expect(cb.y).toBeLessThan(sb.y);
});

test("mobile keeps the stops and the same content (#929)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "phone layout");
  await stub(page);
  await page.goto("/pulse");
  await page.getByTestId("stop-objectives").click();
  await expect(page.getByTestId("no-mission-objectives")).toContainText(
    /objectives belong to a mission/i,
  );
});

test("a store failure does NOT get the first-run treatment (#929)", async ({
  page,
}) => {
  // The gate is `listLoaded && !storeError && !archived && missions.length === 0`, and this is
  // the clause that matters most. An empty rail because the store could not be READ is not a
  // fresh install — telling that operator to "start your first mission" would be inventing an
  // answer out of an absence, which is the mistake the mission work has already paid for twice.
  await page.setViewportSize({ width: 1600, height: 900 });
  await stub(page, { storeError: "the mission store could not be read" });
  await page.goto("/pulse");

  await expect(page.getByTestId("console-store-error")).toBeVisible();
  await expect(page.getByTestId("first-run")).toHaveCount(0);
});

/** The four defects the first review of PR #930 found. Each is a real-browser test because each
 *  is about WHEN a render happens relative to a request landing — a thing a DOM emulator with
 *  synchronous fakes cannot put in the wrong order. */
test.describe("#930 review 1 — the console under a slow store", () => {
  test("a draft survives the empty mission list arriving (finding 1)", async ({
    page,
  }) => {
    // The composer used to be two mounts either side of `firstRun`. Typing while the list was
    // still in flight and then having it resolve EMPTY flipped the flag, unmounted the mount
    // being typed into, and mounted a fresh one — silently discarding the draft with no
    // navigation and nothing on screen to explain it.
    await page.setViewportSize({ width: 1280, height: 900 });
    const gate = deferred();
    await stub(page, {
      missionsResolver: async () => {
        await gate.promise;
        return { missions: [], total: 0, error: null };
      },
    });

    await page.goto("/pulse");
    const box = page.getByRole("textbox").first();
    await box.waitFor();
    await box.fill("what happened to the parser work");
    await expect(box).toHaveValue("what happened to the parser work");

    // The answer lands: an empty list, which is what turns first-run on.
    gate.resolve();
    await expect(page.getByTestId("first-run")).toBeVisible();

    // RED before the fix: the value is "" here, because this is a different element.
    await expect(page.getByRole("textbox").first()).toHaveValue(
      "what happened to the parser work",
    );
  });

  for (const width of [1399, 1600]) {
    test(`zero missions AND zero sessions still answer the tabs at ${width}px (finding 3)`, async ({
      page,
    }) => {
      // With no sessions the console selects `null`, not the UNTRACKED sentinel — a different
      // branch, and the one an operator sees on a brand-new install. The tabs moved `stop` and
      // nothing read it, so both buttons were inert on the emptiest page in the product.
      await page.setViewportSize({ width, height: 900 });
      await stub(page, { cards: [] });
      await page.goto("/pulse");
      await page.getByTestId("pane").waitFor();

      await page.getByTestId("stop-objectives").click();
      await expect(page.getByTestId("no-mission-objectives")).toContainText(
        /objectives belong to a mission/i,
      );

      await page.getByTestId("stop-timeline").click();
      await expect(page.getByTestId("no-mission-timeline")).toContainText(
        /timeline belongs to a mission/i,
      );
    });
  }

  test("switching scope does not invite a first mission on the old scope's evidence (finding 4)", async ({
    page,
  }) => {
    // `listLoaded` was set once and never cleared, so an Archived → Active flip satisfied
    // `firstRun` from the ARCHIVED read while Active was still in flight: the console told the
    // operator to start their first mission without having asked whether they had any.
    await page.setViewportSize({ width: 1280, height: 900 });
    let holdActive = false;
    const gate = deferred();
    // The resolver form, so the helper's per-mission routes keep their precedence — and it is
    // handed the query string, which is what distinguishes the archived read from the active one.
    await stub(page, {
      missionsResolver: async (q) => {
        const archived = q.get("archived") === "1" || q.get("archived") === "true";
        if (!archived && holdActive) await gate.promise;
        return { missions: [], total: 0, error: null };
      },
    });

    await page.goto("/pulse");
    await expect(page.getByTestId("first-run")).toBeVisible();

    // Into the archived scope, which answers; then back, with Active parked.
    const scope = page.getByTestId("rail-scope"); // "Show archived" / "Show active"
    await scope.click();
    await expect(page.getByTestId("first-run")).toHaveCount(0);
    holdActive = true;
    await scope.click();

    // RED before the fix: the invitation is already back, on a read that has not happened.
    await expect(page.getByTestId("first-run")).toHaveCount(0);

    gate.resolve();
    await expect(page.getByTestId("first-run")).toBeVisible();
  });
});

/** #930 review 2 — two lifecycle defects that the FIRST round's fixes introduced. Both are
 *  about state surviving a transition it should not, so both are driven as round trips rather
 *  than as single renders. */
test.describe("#930 review 2 — the stop strip's own lifecycle", () => {
  for (const mode of ["ask", "new mission"] as const) {
    test(`a ${mode} draft survives an OBJECTIVES round trip (finding 1)`, async ({
      page,
    }) => {
      // The explanatory pane used to be a SIBLING branch, so pressing a tab replaced the whole
      // pane — composer included — and coming back mounted a fresh one. An operator lost what
      // they had typed by looking at something.
      await page.setViewportSize({ width: 1280, height: 900 });
      await stub(page);
      await page.goto("/pulse");
      const box = page.getByRole("textbox").first();
      await box.waitFor();

      if (mode === "new mission") {
        await page.getByTestId("composer-mode-new").click();
      }
      const draft = `draft for ${mode}`;
      await box.fill(draft);
      await expect(box).toHaveValue(draft);

      await page.getByTestId("stop-objectives").click();
      await expect(page.getByTestId("no-mission-objectives")).toBeVisible();
      // The composer is still MOUNTED while the explanatory pane shows — that is the fix.
      await expect(page.getByRole("textbox").first()).toHaveValue(draft);

      await page.getByTestId("stop-thread").click();
      // RED before the fix: "" here, with no navigation and no scope change.
      await expect(page.getByRole("textbox").first()).toHaveValue(draft);
      if (mode === "new mission") {
        // …and the MODE comes back too, not just the text.
        await expect(page.getByTestId("composer-mode-new")).toHaveAttribute(
          "aria-pressed",
          "true",
        );
      }
    });
  }

  test("an IN-FLIGHT send survives the round trip, and is not sent twice (finding 1)", async ({
    page,
  }) => {
    // The earlier version of this test parked `/api/pulse/chat` and never pressed SEND, so
    // nothing was ever in flight and its name was a claim it did not test. This one submits to
    // `/api/pulse/ask` — the endpoint the composer actually calls — holds the answer open across
    // a tab round trip, and checks the three things a remount would each destroy: the busy
    // state, a draft typed while busy, and the answer arriving to the same mounted component.
    await page.setViewportSize({ width: 1280, height: 900 });
    await stub(page);
    const gate = deferred();
    let asks = 0;
    await page.route("**/api/pulse/ask**", async (r) => {
      asks += 1;
      await gate.promise;
      await r.fulfill({
        json: {
          answer: "the held answer",
          matches: [],
          stage: "catalog",
          configured: true,
        },
      });
    });

    await page.goto("/pulse");
    const box = page.getByTestId("composer-input");
    await box.waitFor();
    await box.fill("a question mid-flight");
    await page.getByTestId("composer-send").click();

    // In flight: the send is busy and the request has left.
    await expect(page.getByTestId("composer-send")).toBeDisabled();
    await expect.poll(() => asks).toBe(1);

    // A draft typed WHILE the first is still running — the thing a remount would drop.
    await box.fill("and a second thought");

    await page.getByTestId("stop-timeline").click();
    await expect(page.getByTestId("no-mission-timeline")).toBeVisible();
    await page.getByTestId("stop-thread").click();

    // Same instance: busy state and draft both intact, and no duplicate request was issued by
    // a fresh mount replaying its state.
    await expect(page.getByTestId("composer-send")).toBeDisabled();
    await expect(page.getByTestId("composer-input")).toHaveValue(
      "and a second thought",
    );
    expect(asks).toBe(1);

    // And the answer lands on the component that asked for it.
    gate.resolve();
    await expect(page.getByText("the held answer")).toBeVisible();
    expect(asks).toBe(1);
  });

  test("a mission ENTERED without a click lands on its thread, not a stale stop (finding 2)", async ({
    page,
  }) => {
    // At 1600px the tabs are hidden once a detail column exists. If the stop survives an
    // automatic entry, MissionBody renders OBJECTIVES in the main pane with no tab to leave
    // it — the thread and its composer unreachable. Deliberately NO rail click: clicking is
    // what `select()` already resets, and doing it here would mask the defect.
    await page.setViewportSize({ width: 1600, height: 900 });
    const gate = deferred();
    await stub(page, {
      missionsResolver: async () => {
        await gate.promise;
        return missionList([missionRow({ id: "m1", title: "the first one" })]);
      },
    });

    await page.goto("/pulse");
    await page.getByTestId("pane").waitFor();
    await page.getByTestId("stop-objectives").click();
    await expect(page.getByTestId("no-mission-objectives")).toBeVisible();

    // The list lands, and the mission is entered by derivation rather than by selection.
    // (The mission's own lifecycle state is the honest witness that a mission was entered — it
    // renders only from the detail read, so it cannot appear for a mission nobody is on. The
    // detail column that used to play this role was deleted by #942.)
    gate.resolve();
    await expect(page.getByTestId("mission-state")).toBeVisible();

    // RED before the fix: the pane is still showing OBJECTIVES while the tab strip that could
    // leave it was hidden behind the detail column, so the thread and its composer were absent
    // with nothing on screen to get back to them. The strip no longer hides, but entering a
    // mission on the previous mission's tab is still stale state, so the reset still matters.
    //
    // Asserted on the MISSION composer specifically. A bare `getByRole("textbox")` passes
    // against the defect — the app shell's own "Search titles…" box is a visible textbox on
    // this page, so the assertion was satisfied by furniture rather than by the thread.
    await expect(
      page.getByLabel("Send a message to this mission"),
    ).toBeVisible();
    // And the main pane is the THREAD, not the stop the operator left behind — asserted on the
    // pane's own content rather than on a sibling column that no longer exists.
    await expect(page.getByTestId("stop-thread")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(page.getByTestId("objectives")).toHaveCount(0);
  });
});
