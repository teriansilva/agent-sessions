import { expect, test, type Page } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";

import { mockMissions } from "./mission-console";

// Real-browser checks for Pulse orchestration (#726 Phases 1–2). Network is fully mocked —
// the suite never talks to a backend or an AI endpoint.
//
// jsdom can't prove any of these: the approve button is a real tap target on a real emulated
// phone, the evidence disclosure is real layout, and the stale (409) path is the one an
// operator will actually hit — the session moved on between the proposal and the tap, so
// nothing was written. A green unit test on a broken tap is exactly the failure mode the
// workflow's UI rule exists to stop.
//
// WHERE THE CONTROLS RENDER (#1049): nowhere. A decision for a session no mission holds rode that
// session's row under /mission until #948 P3, then the session pane's decision strip. The strip is
// gone — opening the pane was what invalidated its own Approve — so a mission-less decision now has
// no operator surface at all, and the tests that drove one through the pane went with it. What
// remains here is the autonomy copy, which is asserted where the operator actually sets it.



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
          scan_depth: "fast",
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
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        cache_version: 2,
        generated_at: null,
        window_days: 3,
        scan_depth: "fast",
        input_fingerprint: null,
        synthesis_skipped: false,
        cards: [],
      },
    }),
  );
});

function mockOrchestrator(page: Page, pending: unknown[], feed: unknown[] = []) {
  // `feed` defaults to empty on purpose: an action present in BOTH lists renders twice, and a
  // spec that then matches "the approve button" is asserting against an accident.
  // Mutable so the route models the REAL transition: once an action settles it stops being
  // pending, so the re-read after approve/reject must stop returning it. A frozen mock left the
  // Approve button on screen forever and asserted a state the server cannot produce.
  let live = [...(pending as Record<string, unknown>[])];
  // Observed via `page.on("request")`, NOT a route. Each test registers its own
  // approve/reject responder AFTER this helper, and Playwright matches routes newest-first —
  // so that responder's `fulfill()` ends routing and a mutation route registered here would
  // never run. Listening to the request instead is independent of route order.
  page.on("request", (req) => {
    const m = /\/api\/pulse\/actions\/(.+)\/(approve|reject)$/.exec(
      new URL(req.url()).pathname,
    );
    if (m) live = live.filter((a) => a.id !== m[1]);
  });
  return page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    r.fulfill({
      json: {
        config: ORCH_CONFIG,
        pending: live,
        feed,
        expired_now: 0,
        running: [],
        last: {},
      },
    }),
  );
}

test("the autonomy copy says what YOLO can type, not just the tier — in Settings (#929, #983)", async ({
  page,
}) => {
  await mockOrchestrator(page, []);
  await mockMissions(page);
  // #929 moved every autonomy control off the route and into Settings, so this is asserted
  // where the operator now sets it. The property under test is unchanged and is the reason the
  // copy exists: "YOLO" alone reads as "does everything", so it has to say what it can send.
  // #983 changed WHICH operator text that is (an objective's direction, or the default nudge),
  // so the copy now names authorship rather than the `continue` verb.
  await page.goto(settingsPath("ai-mission-control"));
  const ceiling = page.getByText(/only ever types text/i);
  await expect(ceiling).toBeVisible();
  await expect(ceiling).toContainText("you wrote");
  await expect(ceiling).toContainText("The AI decides when, never what.");
  await expect(ceiling).toContainText(/always waits for your approval/i);

  // And it is genuinely gone from the route — a second copy drifting out of sync with the
  // control is what #929 is about.
  await page.goto("/mission");
  await expect(page.getByText(/acts on its own:/i)).toHaveCount(0);
});
