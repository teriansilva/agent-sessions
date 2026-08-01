import { expect, test } from "@playwright/test";

// #754 — the orchestrator queue merged into the session cards, plus project/agent filters.
//
// Measured against the live stores before this change: 16 sessions had a live action and all 16
// already appeared under "Needs you", with 0 exclusive to the queue. So the queue rendered one
// session twice, in two visual languages, with two different affordances.

const NOW = Math.floor(Date.now() / 1000);

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
  prompt: "p",
  notify: "escalations",
  configured: true,
  default_prompt: "p",
  default_nudge_template: "Please continue.",
};

const ACTION = {
  id: "act-1",
  state: "proposed",
  ts: NOW,
  expires_at: NOW + 1800,
  tier: "suggest",
  session_id: "claude:aaa",
  engine: "claude",
  title: "Switch the default model",
  project: "infra",
  project_id: "p1",
  verb: "continue",
  confidence: 0.86,
  rationale: "finished the edit and stopped without confirming",
  evidence: "none",
};

function card(over: Record<string, unknown> = {}) {
  return {
    id: "claude:aaa",
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
    r.fulfill({
      json: {
        config: ORCH_CONFIG,
        pending: [ACTION],
        feed: [],
        expired_now: 0,
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
        cards: [
          card({ pending_action: ACTION }),
          card({
            id: "codex:bbb",
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
          card({ id: "claude:ccc", title: "Docs pass", state: "idle" }),
        ],
      },
    }),
  );
});

test("a session appears ONCE, with its decision controls on the card", async ({
  page,
}) => {
  await page.goto("/pulse");
  await expect(page.getByRole("heading", { name: /pulse/i })).toBeVisible();

  // The action's own title appears exactly once on the page — the queue used to render it a
  // second time in its own list.
  await expect(page.getByText("Switch the default model")).toHaveCount(1);

  // …and the controls are ON that card, not in a separate block.
  const approve = page.getByRole("button", { name: /^approve$/i });
  await expect(approve).toHaveCount(1);
  const cardBox = (await page
    .locator("li", { hasText: "Switch the default model" })
    .first()
    .boundingBox())!;
  const btnBox = (await approve.boundingBox())!;
  expect(btnBox.y).toBeGreaterThan(cardBox.y);
  expect(btnBox.y).toBeLessThan(cardBox.y + cardBox.height);
});

test("a card with a live action sorts above one without", async ({ page }) => {
  await page.goto("/pulse");
  await expect(page.getByRole("button", { name: /^approve$/i })).toBeVisible();
  const titles = await page.locator("li").allInnerTexts();
  const withAction = titles.findIndex((t) =>
    t.includes("Switch the default model"),
  );
  const without = titles.findIndex((t) => t.includes("Relay cap"));
  expect(withAction).toBeGreaterThanOrEqual(0);
  expect(without).toBeGreaterThan(withAction);
});

test("project and agent filters narrow the whole list and compose", async ({
  page,
}) => {
  await page.goto("/pulse");
  await expect(page.getByText("Relay cap")).toBeVisible();

  // Counts come from the unfiltered set, so a chip states what selecting it would yield.
  await page.getByRole("button", { name: /^battlelab\s+1$/i }).click();
  await expect(page.getByText("Relay cap")).toBeVisible();
  await expect(page.getByText("Switch the default model")).toHaveCount(0);

  // …and the chip is still there with the same count after filtering.
  await expect(
    page.getByRole("button", { name: /^battlelab\s+1$/i }),
  ).toBeVisible();

  await page.getByRole("button", { name: /clear filters/i }).click();
  await expect(page.getByText("Switch the default model")).toBeVisible();
});

test("a manual pass puts its new actions onto the cards, not just in the panel", async ({
  page,
}) => {
  // The panel says "N actions need you — shown on the session cards below". Without telling the
  // page to reload, that claim was false until a manual refresh: the pass created the action
  // but no card carried it (#754 review).
  let scanned = false;
  await page.unroute(/\/api\/pulse$/);
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
        // Before the pass the session has no action; after it, the card carries one.
        cards: [
          scanned ? card({ pending_action: ACTION }) : card({ state: "idle" }),
        ],
      },
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

  await page.goto("/pulse");
  await expect(page.getByRole("button", { name: /^approve$/i })).toHaveCount(0);

  await page.getByRole("button", { name: /run now/i }).click();

  // The card gains the control without a reload.
  await expect(page.getByRole("button", { name: /^approve$/i })).toHaveCount(1);
});

test("resolving from a card also updates the panel's count and feed", async ({
  page,
}) => {
  // The panel owns its own pending/feed. Without telling it, approving on a card left
  // "1 action needs you" sitting above a card that no longer had one (#754 review).
  let settled = false;
  await page.unroute(/\/api\/pulse$/);
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
        cards: [settled ? card({}) : card({ pending_action: ACTION })],
      },
    }),
  );
  await page.unroute(/\/api\/pulse\/orchestrator$/);
  await page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    r.fulfill({
      json: {
        config: ORCH_CONFIG,
        pending: settled ? [] : [ACTION],
        feed: settled ? [{ ...ACTION, state: "delivered" }] : [],
        expired_now: 0,
        running: [],
        last: {},
      },
    }),
  );
  await page.route(/\/api\/pulse\/actions\/.*\/approve$/, async (r) => {
    settled = true;
    await r.fulfill({ json: { ...ACTION, state: "delivered" } });
  });

  // Count the panel's OWN fetches. Asserting on its rendered text instead was useless: the
  // text also disappears if the component happens to remount, so the assertion passed with the
  // wiring removed. This measures the thing the fix actually does.
  let orchFetches = 0;
  page.on("request", (req) => {
    if (/\/api\/pulse\/orchestrator$/.test(new URL(req.url()).pathname))
      orchFetches += 1;
  });

  await page.goto("/pulse");
  await expect(page.getByText(/1 action needs you/i)).toBeVisible();
  const before = orchFetches;

  await page.getByRole("button", { name: /^approve$/i }).click();
  await expect(page.getByRole("button", { name: /^approve$/i })).toHaveCount(0);

  // The panel re-reads its own state, so its count and feed cannot disagree with the cards.
  await expect.poll(() => orchFetches).toBeGreaterThan(before);
  await expect(page.getByText(/action needs? you/i)).toHaveCount(0);
});

test("a settled action loses its controls even when the background refresh fails", async ({
  page,
}) => {
  // Approve succeeds; the refetch that was supposed to remove the row does not. Its catch is
  // silent by design, so the stale `pending_action` survived while `ActionRow` cleared `busy`
  // in its `finally` — leaving Approve/Reject enabled for an action the server had already
  // decided, until the operator reloaded the page (#762 review). The response already says
  // what happened, so the settlement is applied locally and the refetch only reconciles.
  let approved = false;
  await page.unroute(/\/api\/pulse$/);
  await page.route(/\/api\/pulse$/, async (r) => {
    if (approved) return r.fulfill({ status: 500, json: { detail: "boom" } });
    await r.fulfill({
      json: {
        cache_version: 2,
        generated_at: NOW,
        window_days: 3,
        scan_depth: "slow",
        input_fingerprint: null,
        synthesis_skipped: false,
        banner: null,
        cards: [
          // Exactly what `_attach_pending` emits: re-banded to `needs_you`, with the band it
          // had before the overlay preserved so the client can put it back.
          card({
            pending_action: ACTION,
            state: "needs_you",
            state_without_action: "idle",
          }),
        ],
      },
    });
  });
  await page.route(/\/api\/pulse\/actions\/.*\/approve$/, async (r) => {
    approved = true;
    await r.fulfill({ json: { ...ACTION, state: "delivered" } });
  });

  await page.goto("/pulse");
  const approve = page.getByRole("button", { name: /^approve$/i });
  await expect(approve).toBeVisible();
  await approve.click();

  // Two assertions this test needs to be worth anything:
  //
  // The CARD must still be there. Without that, a blanked overview (a 500 body applied as if it
  // were an overview) removes the controls too and the test passes for the wrong reason.
  await expect(page.getByText("Switch the default model")).toBeVisible();
  // And the check must be on something STABLE. `Approve` relabels itself to `Sending…` while
  // the request is in flight, so asserting the button is gone passes during that window —
  // against the unfixed code as well, which is exactly how this test first fooled me. The
  // action's rationale only leaves the DOM when the row itself does.
  await expect(page.getByText(/finished the edit and stopped/i)).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^approve$/i })).toHaveCount(0);

  // The BAND has to go back too. `_attach_pending` re-banded this card to `needs_you` because
  // of the action; with the action gone it belongs under its own state again, or the session
  // reads as needing you with nothing pending until a later fetch succeeds — and the fetch
  // failing is precisely the case this branch exists for.
  //
  // The band used to be a section heading; since #754 it is the card's LED accessible name,
  // so that is where this asserts it now — same fact, current structure.
  await expect(page.getByRole("img", { name: "Needs you" })).toHaveCount(0);
  await expect(page.getByRole("img", { name: "Idle" })).toHaveCount(1);
});

test("a card that existed only for its action goes away with it", async ({
  page,
}) => {
  // `_attach_pending` synthesizes a card when a live action has no cached card. Settle that
  // action and there is nothing left to show: no summary, no controls, no real session row —
  // just an empty phantom sitting under "Needs you" (#762 review).
  let approved = false;
  await page.unroute(/\/api\/pulse$/);
  await page.route(/\/api\/pulse$/, async (r) => {
    if (approved) return r.fulfill({ status: 500, json: { detail: "boom" } });
    await r.fulfill({
      json: {
        cache_version: 2,
        generated_at: NOW,
        window_days: 3,
        scan_depth: "slow",
        input_fingerprint: null,
        synthesis_skipped: false,
        banner: null,
        cards: [
          card({ state: "idle" }),
          {
            id: "codex:ddd",
            engine: "codex",
            title: "Phantom candidate",
            cwd: "",
            project: { kind: "project", id: "p3", name: "relay" },
            state: "needs_you",
            synthesized_for_action: true,
            live: false,
            last_activity: NOW - 60,
            intervention_required: false,
            intervention_reason: "",
            ai_summary: "",
            synthesis: "",
            pending_action: { ...ACTION, id: "act-2", session_id: "codex:ddd" },
          },
        ],
      },
    });
  });
  await page.route(/\/api\/pulse\/actions\/.*\/approve$/, async (r) => {
    approved = true;
    await r.fulfill({ json: { ...ACTION, id: "act-2", state: "delivered" } });
  });

  await page.goto("/pulse");
  await expect(page.getByText("Phantom candidate")).toBeVisible();

  await page.getByRole("button", { name: /^approve$/i }).click();

  await expect(page.getByText("Phantom candidate")).toHaveCount(0);
  // The real session is untouched.
  await expect(page.getByText("Switch the default model")).toBeVisible();
});
