import { expect, test } from "@playwright/test";

// #754 (filters half) — narrow the Pulse card list by project and agent.
//
// The fixture deliberately carries TWO distinct projects both named `app`, under different
// parents. That is not a contrived case: the project store allows duplicate names and folder
// refs share basenames, and keying the chips by display name merged them into a single chip
// that then showed both projects' sessions.

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

function proj(id: string, name: string) {
  return { kind: "project", id, name, color: "#ffb000" };
}

// infra: 2 (one claude, one codex) · app·a: 1 claude · app·b: 1 codex · battlelab: 1 codex
const CARDS = [
  card({}),
  card({
    id: "codex:bbb",
    engine: "codex",
    title: "Relay cap",
    cwd: "/home/u/battlelab",
    project: proj("p2", "battlelab"),
  }),
  card({
    id: "codex:ccc",
    engine: "codex",
    title: "Docs pass",
    state: "idle",
  }),
  card({
    id: "claude:ddd",
    engine: "claude",
    title: "App one",
    cwd: "/work/a/app",
    project: proj("p3", "app"),
  }),
  card({
    id: "codex:eee",
    engine: "codex",
    title: "App two",
    cwd: "/work/b/app",
    project: proj("p4", "app"),
  }),
];

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
        pending: [],
        feed: [],
        expired_now: 0,
        running: [],
        last: {},
      },
    }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: overview(CARDS) }),
  );
});

test("project and agent filters narrow the whole list", async ({ page }) => {
  await page.goto("/pulse");
  await expect(page.getByText("Relay cap")).toBeVisible();

  // The orchestrator queue is deliberately empty here: it is a separate list on this branch and
  // is not what these filters govern, so leaving an action in it would have the same title
  // appear twice and the assertion would be measuring the wrong surface.
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

test("two projects with the same name are two chips, not one", async ({
  page,
}) => {
  // Keyed by display name, `/work/a/app` and `/work/b/app` collapsed into one `app 2` chip and
  // selecting it showed both. Keyed by id they are separate, and the parent disambiguates them.
  await page.goto("/pulse");
  await expect(
    page.getByRole("button", { name: /^app · a\s+1$/i }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /^app · b\s+1$/i }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /^app\s+2$/i })).toHaveCount(0);

  await page.getByRole("button", { name: /^app · a\s+1$/i }).click();
  await expect(page.getByText("App one")).toBeVisible();
  await expect(page.getByText("App two")).toHaveCount(0);
});

test("project and agent compose to their intersection", async ({ page }) => {
  await page.goto("/pulse");

  // infra holds two sessions, one per engine.
  await page.getByRole("button", { name: /^infra\s+2$/i }).click();
  await expect(page.getByText("Switch the default model")).toBeVisible();
  await expect(page.getByText("Docs pass")).toBeVisible();

  // Adding the agent narrows it further rather than replacing the project filter.
  await page.getByRole("button", { name: /^codex\s+3$/i }).click();
  await expect(page.getByText("Docs pass")).toBeVisible();
  await expect(page.getByText("Switch the default model")).toHaveCount(0);
  await expect(page.getByText("App two")).toHaveCount(0); // still inside infra

  // Both chips read as pressed — the state is not carried by colour alone.
  await expect(
    page.getByRole("button", { name: /^infra\s+2$/i }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.getByRole("button", { name: /^codex\s+3$/i }),
  ).toHaveAttribute("aria-pressed", "true");
});

test("a combination that matches nothing says so and offers one-action recovery", async ({
  page,
}) => {
  await page.goto("/pulse");
  // battlelab is codex-only, so this intersection is empty — the list area used to just go blank.
  await page.getByRole("button", { name: /^battlelab\s+1$/i }).click();
  await page.getByRole("button", { name: /^claude\s+2$/i }).click();

  await expect(
    page.getByText(/no sessions match these filters/i),
  ).toBeVisible();
  await page.getByRole("button", { name: /show all sessions/i }).click();
  await expect(page.getByText("Relay cap")).toBeVisible();
  await expect(page.getByText("Switch the default model")).toBeVisible();
});

test("a scan that invalidates the selection does not leave a blank page", async ({
  page,
}) => {
  // The worse half of the same defect: after a scan the selected project may not exist, and if
  // the new overview has too few facets to draw the filter row there is no chip left to undo it.
  let scanned = false;
  await page.unroute(/\/api\/pulse$/);
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: overview(
        scanned
          ? [
              card({
                id: "claude:zzz",
                title: "Only one left",
                project: proj("p9", "solo"),
              }),
            ]
          : CARDS,
      ),
    }),
  );
  await page.route(/\/api\/pulse\/scan$/, async (r) => {
    scanned = true;
    await r.fulfill({
      json: overview([
        card({
          id: "claude:zzz",
          title: "Only one left",
          project: proj("p9", "solo"),
        }),
      ]),
    });
  });

  await page.goto("/pulse");
  await page.getByRole("button", { name: /^battlelab\s+1$/i }).click();
  await expect(page.getByText("Relay cap")).toBeVisible();

  await page.getByRole("button", { name: /scan now/i }).click();

  // The stale `battlelab` selection is reconciled away, so the surviving session is visible.
  await expect(page.getByText("Only one left")).toBeVisible();
  await expect(page.getByText(/no sessions match these filters/i)).toHaveCount(
    0,
  );
});
