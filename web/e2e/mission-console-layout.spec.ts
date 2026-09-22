/** #929 → #948 P3 — the console's layout under the front-door rework.
 *
 *  These are real-browser tests because the findings they guard are LAYOUT and TIMING facts a DOM
 *  emulator cannot answer: a media query, what an operator can reach at a given width, and WHEN a
 *  render happens relative to a request landing.
 *
 *  #948 P3 removed three surfaces this file used to exercise: the no-mission "first-run" block, the
 *  Conversation / Details tab strip (and its "choose one from the list" explanatory pane), and the
 *  untracked-session list. Each test below that survived is rewritten against what replaced them —
 *  the new-mission landing (`mission-landing`), the rail's own empty state (`rail-no-missions`) and
 *  the one details disclosure (`details-toggle`). The invariant is still reachability of CONTENT,
 *  never the presence of a control.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  MISSION,
  flipMissionScope,
  missionList,
  missionRow,
  mockMissions,
  openMissionConversation,
  openMissionDetails,
  openMissionRail,
} from "./mission-console";

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
/** A live session no mission holds. Since #948 P3 it renders nothing under `/mission` (its
 *  decisions go to the session's own pane); it stays in the default fixture so that no layout
 *  assertion here quietly depends on the overview being empty. */
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
  scan_depth: "fast",
  input_fingerprint: "fp",
  synthesis_skipped: false,
  cards: [CARD],
};

/** `missions: []` is a SUCCESSFUL read that found nothing — the fresh-install case. */
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
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { ...OVERVIEW, cards: opts.cards ?? [CARD] } }),
  );
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
  // `missionList(...)` — NOT a bare array. `mockMissions` does `m ?? EMPTY_MISSIONS`, and `[]`
  // is not nullish, so passing one fulfils with an ARRAY where the console expects
  // `{missions, total, …}`; the parse fails, no list is applied, and `listLoaded` never flips.
  // That silently defeats every "the list has landed" witness below rather than failing honestly.
  await mockMissions(page, {
    missions:
      opts.missionsResolver ?? missionList(rows, opts.storeError ?? null),
  });
}

/** Select a mission from the rail. Nothing is auto-selected since #948 P3 — the section opens on
 *  the new-mission page — so a test about a selected mission picks one itself, on either project. */
async function selectFromRail(page: Page, title: RegExp) {
  await openMissionRail(page);
  await page
    .getByRole("navigation", { name: /missions/i })
    .getByRole("button", { name: title })
    .first()
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByTestId("mission-state")).toBeVisible();
}

test.describe("the workspace's content is reachable at every width (#929, #948)", () => {
  for (const width of [1399, 1400, 1600]) {
    test(`no mission selected at ${width}px: the new-mission page is the content, with nothing to disclose`, async ({
      page,
    }) => {
      // #929's defect was a no-mission page whose content depended on the width: at 1400+ the
      // operator saw neither the stops nor what they led to. The no-mission page is now the
      // new-mission landing (#948 P3), and the width still must not decide what it offers.
      await page.setViewportSize({ width, height: 900 });
      await stub(page);
      await page.goto("/mission");

      await expect(page.getByTestId("mission-landing")).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "What should this mission achieve?" }),
      ).toBeVisible();
      await expect(page.getByTestId("new-mission-form")).toBeVisible();
      await expect(page.getByTestId("new-mission-instruction")).toBeInViewport();
      // Nothing is selected, so there is no mission header and no details to disclose.
      await expect(page.getByTestId("console-title")).toHaveCount(0);
      await expect(page.getByTestId("details-toggle")).toHaveCount(0);
      await expect(page.getByTestId("mission-details")).toHaveCount(0);
    });
  }

  test("with a mission selected at 1600px the details sit beside the thread, with no disclosure (#942, #948)", async ({
    page,
  }) => {
    // #942 deleted the old detail column; #948 P3 deleted the tab strip that replaced it. At 1400+
    // the details are simply beside the thread, and the below-1400 disclosure is not shown.
    await page.setViewportSize({ width: 1600, height: 900 });
    await stub(page, { missions: [missionRow()] });
    await page.goto("/mission");
    await selectFromRail(page, /Kimi transcript adapter/i);
    await expect(page.getByTestId("detail-column")).toHaveCount(0);
    await expect(page.getByTestId("mission-details")).toBeVisible();
    await expect(page.getByTestId("detail-context")).toBeVisible();
    await expect(page.getByTestId("details-toggle")).toBeHidden();
    const pane = (await page.getByTestId("pane").boundingBox())!;
    const details = (await page.getByTestId("mission-details").boundingBox())!;
    expect(details.x).toBeGreaterThanOrEqual(pane.x + pane.width);
  });
});

test("a fresh install leads with the composer (#929, #948)", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await stub(page);
  await page.goto("/mission");

  // A READ that found nothing — the rail says so — not a list still in flight.
  await expect(page.getByTestId("rail-no-missions")).toBeVisible();

  // ASSERTED AS GEOMETRY, NOT AS DOM NESTING (#930 review 1, finding 1). "Leads with the composer"
  // is a layout claim: the heading that explains it, then the composer, above the fold. (It was
  // also measured against the untracked-session list below it; that list was removed by #948 P3.)
  const heading = page.getByRole("heading", {
    name: "What should this mission achieve?",
  });
  const composer = page.getByTestId("new-mission-form");
  await expect(heading).toBeVisible();
  await expect(composer).toBeVisible();
  const [hb, cb] = [
    (await heading.boundingBox())!,
    (await composer.boundingBox())!,
  ];
  expect(cb.y).toBeGreaterThan(hb.y);
  expect(cb.y + cb.height).toBeLessThanOrEqual(900);
  // Nothing needs the operator and nothing is filtered, so there is no NEEDS YOU section either.
  await expect(page.getByTestId("landing-needs-you")).toHaveCount(0);
});

test("a store failure is not read as 'you have no missions' (#929)", async ({
  page,
}) => {
  // An empty rail because the store could not be READ is not a fresh install. The first-run block
  // that used to get this wrong is gone (#948 P3); what is left that could invent an answer out of
  // an absence is the rail's "no missions" empty state and the landing's NEEDS YOU preview.
  await page.setViewportSize({ width: 1600, height: 900 });
  await stub(page, { storeError: "the mission store could not be read" });
  await page.goto("/mission");

  await expect(page.getByTestId("console-store-error")).toBeVisible();
  await expect(page.getByTestId("rail-no-missions")).toHaveCount(0);
  await expect(page.getByTestId("landing-needs-empty")).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText(/nothing needs you/i);
});

/** The defects the first review of PR #930 found. Each is about WHEN a render happens relative to
 *  a request landing — a thing a DOM emulator with synchronous fakes cannot put in the wrong order. */
test.describe("#930 review 1 — the console under a slow store", () => {
  // ONE mode since #1058: the landing's ASK half became `/ask`, a route with no mission list to
  // arrive underneath it, so the parameterised "ask" case has no list to survive. The defect was
  // never about which mode was on screen — it was about the list's arrival remounting the composer
  // — and the brief form is the composer that is still mounted there.
  for (const mode of ["new mission"] as const) {
    test(`a ${mode} draft survives the empty mission list arriving (finding 1)`, async ({
      page,
    }) => {
      // The composer used to be two mounts either side of the first-run flag. Typing while the list
      // was still in flight and then having it resolve EMPTY unmounted the field being typed into
      // and mounted a fresh one — silently discarding the draft. The landing keeps ONE composer
      // mount (#948 P3), and this pins that the list arriving does not replace it.
      await page.setViewportSize({ width: 1280, height: 900 });
      const gate = deferred();
      await stub(page, {
        missionsResolver: async () => {
          await gate.promise;
          return missionList([]);
        },
      });

      await page.goto("/mission");
      const field = page.getByTestId("new-mission-instruction");
      await field.waitFor();
      const draft = "what happened to the parser work";
      await field.fill(draft);
      await expect(field).toHaveValue(draft);
      const node = await field.elementHandle();

      // The answer lands: an empty list, which the rail announces.
      gate.resolve();
      await expect(page.getByTestId("rail-no-missions")).toBeAttached();

      // RED if the list's arrival remounts the composer: the value would be "" and the node gone.
      await expect(field).toHaveValue(draft);
      expect(
        await node!.evaluate(
          (el) => (el as HTMLTextAreaElement).isConnected && (el as HTMLTextAreaElement).value,
        ),
      ).toBe(draft);
    });
  }

  test("switching scope does not declare 'no missions' on the old scope's evidence (finding 4)", async ({
    page,
  }) => {
    // `listLoaded` was set once and never cleared, so an Archived → Active flip satisfied the empty
    // state from the ARCHIVED read while Active was still in flight. The first-run invitation this
    // used to be asserted through is gone (#948 P3); the rail's own "no missions" empty state reads
    // the same flag, so it is the witness now.
    await page.setViewportSize({ width: 1280, height: 900 });
    let holdActive = false;
    const gate = deferred();
    // The resolver form, so the helper's per-mission routes keep their precedence — and it is
    // handed the query string, which is what distinguishes the archived read from the active one.
    await stub(page, {
      missionsResolver: async (q) => {
        const archived =
          q.get("archived") === "1" || q.get("archived") === "true";
        if (!archived && holdActive) await gate.promise;
        return missionList([]);
      },
    });

    await page.goto("/mission");
    await expect(page.getByTestId("rail-no-missions")).toBeAttached();

    // Into the archived scope, which answers; then back, with Active parked.
    await flipMissionScope(page);
    await expect(
      page.locator('[data-testid="rail-scope-archived"]:visible').first(),
    ).toHaveAttribute("aria-selected", "true");
    await expect(page.getByTestId("rail-no-missions")).toBeAttached();
    holdActive = true;
    await flipMissionScope(page);
    await expect(
      page.locator('[data-testid="rail-scope-active"]:visible').first(),
    ).toHaveAttribute("aria-selected", "true");

    // RED before the fix: "no missions" is already back, on a read that has not happened.
    await expect(page.getByTestId("rail-no-missions")).toHaveCount(0);

    gate.resolve();
    await expect(page.getByTestId("rail-no-missions")).toBeAttached();
  });
});

/** #930 review 2 → #948 P3. These were the stop strip's own lifecycle defects: state surviving (or
 *  not surviving) a switch between the thread and the details. The strip is gone; the details are
 *  one disclosure below 1400px that must never unmount or move the thread. Driven as round trips,
 *  on both projects, at a width where the disclosure is in play. */
test.describe("#930 review 2 — the details disclosure's lifecycle", () => {
  test("a draft in the mission's composer survives a DETAILS round trip (finding 1)", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await stub(page, { missions: [missionRow()] });
    await page.goto("/mission");
    await selectFromRail(page, /Kimi transcript adapter/i);

    const box = page.getByLabel("Send a message to this mission");
    const draft = "draft for the mission";
    await box.fill(draft);
    const node = await box.elementHandle();

    await openMissionDetails(page, "objectives");
    await expect(page.getByTestId("mission-details")).toBeVisible();
    // The thread — composer included — stays on screen while the details are open.
    await expect(box).toBeVisible();
    await expect(box).toHaveValue(draft);

    await openMissionConversation(page);
    await expect(page.getByTestId("mission-details")).toBeHidden();
    await expect(box).toHaveValue(draft);
    // The SAME element: no remount across the round trip.
    expect(
      await node!.evaluate(
        (el) => (el as HTMLTextAreaElement).isConnected && (el as HTMLTextAreaElement).value,
      ),
    ).toBe(draft);
  });

  test("an IN-FLIGHT turn survives the round trip, and is not sent twice (finding 1)", async ({
    page,
  }) => {
    // Holds the mission's turn open across a details round trip and checks the three things a
    // remount would each destroy: the busy state, a draft typed while busy, and the settlement
    // arriving to the same mounted component. (It used to do this on the no-mission Ask; that page
    // has no disclosure since #948 P3, and a mission's composer is the one a round trip can reach.)
    await page.setViewportSize({ width: 1280, height: 900 });
    await stub(page, { missions: [missionRow()] });
    const gate = deferred();
    let sends = 0;
    await page.route("**/api/missions/*/message", async (r) => {
      sends += 1;
      await gate.promise;
      await r.fulfill({
        json: { turn_id: "t1", state: "done", answer: "ok", matches: [] },
      });
    });

    await page.goto("/mission");
    await selectFromRail(page, /Kimi transcript adapter/i);
    const box = page.getByTestId("composer-input");
    await box.fill("a question mid-flight");
    await page.getByTestId("composer-send").click();

    // In flight: the send is busy and the request has left.
    await expect(page.getByTestId("composer-send")).toBeDisabled();
    await expect.poll(() => sends).toBe(1);

    // A draft typed WHILE the first is still running — the thing a remount would drop.
    await box.fill("and a second thought");

    await openMissionDetails(page, "objectives");
    await expect(page.getByTestId("mission-details")).toBeVisible();
    await openMissionConversation(page);

    // Same instance: busy state and draft both intact, and no duplicate request.
    await expect(page.getByTestId("composer-send")).toBeDisabled();
    await expect(box).toHaveValue("and a second thought");
    expect(sends).toBe(1);

    // And the settlement lands on the component that asked: busy clears, the draft is still there.
    gate.resolve();
    await expect(page.getByTestId("composer-send")).toBeEnabled();
    await expect(box).toHaveValue("and a second thought");
    expect(sends).toBe(1);
  });

  test("a mission ENTERED by deep link while the details are open still shows its thread (finding 2)", async ({
    page,
  }) => {
    // The old defect: a mission entered without a click inherited the previous view's stop, so the
    // thread and its composer were unreachable. Auto-entry is gone (#948 P3); the entry without a
    // click that remains is `?m=<id>`, and the disclosure's open state is kept for the visit. So
    // enter mission B by deep link with the band still open from mission A, and assert B's thread
    // and composer are on screen — the band never displaces them.
    const A = "msn_" + "a".repeat(32);
    const B = "msn_" + "b".repeat(32);
    await page.setViewportSize({ width: 1280, height: 900 });
    await stub(page, {
      missions: [
        missionRow({ id: A, title: "Mission A" }),
        missionRow({ id: B, title: "Mission B" }),
      ],
    });
    await page.route(/\/api\/missions\/msn_[0-9a-f]{32}(\?.*)?$/, (r) => {
      const id = /msn_[0-9a-f]{32}/.exec(r.request().url())![0];
      return r.fulfill({
        json: {
          ...MISSION,
          id,
          title: id === A ? "Mission A" : "Mission B",
          events: [],
          events_next_seq: null,
        },
      });
    });

    await page.goto(`/mission?m=${A}`);
    await expect(page.getByTestId("console-title")).toHaveText("Mission A");
    await openMissionDetails(page, "objectives");
    await expect(page.getByTestId("details-toggle")).toHaveAttribute(
      "aria-expanded",
      "true",
    );

    // Client-side, the way an in-app link arrives: no reload, no click on the rail.
    await page.evaluate((id) => {
      window.history.pushState({}, "", `/mission?m=${id}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    }, B);
    await expect(page.getByTestId("console-title")).toHaveText("Mission B");

    // Asserted on the MISSION composer specifically. A bare `getByRole("textbox")` passes against
    // the defect — the app shell's own search box is a visible textbox on this page.
    await expect(page.getByTestId("pane")).toBeVisible();
    await expect(
      page.getByLabel("Send a message to this mission"),
    ).toBeInViewport();
  });
});
