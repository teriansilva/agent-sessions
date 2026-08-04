import { expect, test } from "@playwright/test";

// #754 — Pulse must use the width it has.
//
// Measured on the shipped v0.17.2 build at 1900px, with the numbers this spec now pins:
// session cards 294px wide, 5 per row (fine) — activity feed rows 1490px wide, ONE per row,
// content ending around 1000px with `conf` and `Open session` marooned at the right edge.
// The queue this issue opened about was removed in #762; the feed underneath inherited its
// defect verbatim. Separately the cards rendered as four state sections, each leaving a
// partial row.
//
// #777 then merged the feed INTO the cards, so the two feed-density tests that lived here are
// gone with it — one list means the card assertions below are the whole story.
const NOW = Math.floor(Date.now() / 1000);

const ORCH = {
  enabled: true,
  autonomy: "suggest",
  allowed_verbs: ["continue"],
  auto_verbs_ceiling: ["continue"],
  confidence_min: 0.75,
  interval_minutes: 10,
  max_actions_per_pass: 4,
  proposal_ttl_minutes: 30,
  stale_hours: 24,
  nudge_template: "Please continue.",
  prompt: "p",
  notify: "escalations",
  configured: true,
  default_prompt: "p",
  default_nudge_template: "Please continue.",
};

const PROJECTS = [
  ["p1", "infra"],
  ["p2", "battlelab"],
  ["p3", "superstatus"],
  // Neutral names only: `check-public-snapshot` denylists internal repo/host names, and a
  // fixture is snapshot content like any other file.
  ["p4", "sandbox"],
];

function act(i: number, sid: string, title: string) {
  return {
    id: `act-${i}`,
    state: i % 3 === 0 ? "proposed" : "escalated",
    ts: NOW - i * 400,
    expires_at: NOW + 1800,
    tier: "suggest",
    session_id: sid,
    engine: sid.split(":")[0],
    title,
    project: PROJECTS[i % 4][1],
    project_id: PROJECTS[i % 4][0],
    verb: i % 3 === 0 ? "continue" : "escalate",
    confidence: 0.8 + (i % 3) * 0.05,
    rationale:
      "Agent located the classification rule and confirmed the contract but needs a design decision before it can proceed.",
    evidence: "screen",
  };
}

const TITLES = [
  "Awaiting user decision on issue #428 implementation vs alerting gap",
  "Switch OpenCode default model to laguna-s-2.1",
  "Choose alert delivery channel for laguna-s21 monitoring",
  "Awaiting user decision on CAP_SYS_ADMIN grant",
  "Update infra docs to prefer Ideogram over SANA",
  "Resolve 3090 vision+ACE-Step VRAM collision",
  "Verify Mac runner launchd persistence",
  "Add CI check for reserved ModSecurity rule IDs",
  "Prompted to cut a release after merging dictation",
  "Enable captcha and email verification on staging",
  "Resume PR #731 investigation",
];

const CARDS = TITLES.map((t, i) => {
  const eng = ["claude", "codex", "opencode", "gemini"][i % 4];
  const [pid, pname] = PROJECTS[i % 4];
  return {
    id: `${eng}:${"0".repeat(7)}${i}-0000-4000-8000-00000000000${i % 10}`,
    engine: eng,
    title: t,
    cwd: `/home/u/${pname}`,
    project: { kind: "project", id: pid, name: pname, color: "#ffb000" },
    state: i < 6 ? "needs_you" : i < 8 ? "in_flight" : "idle",
    live: i >= 6 && i < 8,
    last_activity: NOW - i * 3600,
    intervention_required: i < 3,
    intervention_reason: i < 3 ? "waiting on a decision" : "",
    reviewed_at: NOW - 600,
    ai_summary:
      "Agent finished the edit, validated the JSON and stopped without confirming the next step.",
    synthesis: null,
    ...(i < 6
      ? {
          pending_action: act(i, `${eng}:x`, t),
          state_without_action: "idle",
        }
      : {}),
  };
});

async function mock(page) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        pulse: {
          auto_enabled: true,
          interval_minutes: 30,
          window_days: 3,
          scan_depth: "slow",
          configured: true,
        },
        orchestrator: ORCH,
      },
    }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "0.17.2" } }),
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
        config: ORCH,
        pending: CARDS.slice(0, 6).map((c) => c.pending_action),
        feed: TITLES.slice(0, 8).map((t, i) => ({
          ...act(i + 20, `claude:f${i}`, t),
          state: ["delivered", "expired", "observed", "rejected"][i % 4],
        })),
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
        banner:
          "Six sessions are waiting on a decision; two are still working.",
        cards: CARDS,
      },
    }),
  );
}

/** Widths + per-row counts for a class-name prefix (CSS modules hash the suffix). */
async function layout(page, prefix: string) {
  return page.evaluate((p) => {
    const els = Array.from(document.querySelectorAll<HTMLElement>("*")).filter(
      (e) =>
        (e.tagName === "LI" || e.tagName === "DIV") &&
        Array.from(e.classList).some((c) => c.startsWith(p)),
    );
    if (!els.length) return { n: 0, width: 0, perRow: 0 };
    const tops = els.map((e) => Math.round(e.getBoundingClientRect().top));
    return {
      n: els.length,
      width: Math.round(els[0].getBoundingClientRect().width),
      perRow: tops.filter((t) => t === tops[0]).length,
    };
  }, prefix);
}

test("the session cards are ONE list, ordered needs-you first", async ({
  page,
}) => {
  await mock(page);
  await page.setViewportSize({ width: 1900, height: 1200 });
  await page.goto("/pulse");
  await expect(page.getByText("Resume PR #731 investigation")).toBeVisible();

  // No per-band sections: the four headings used to break the grid and leave a partial row.
  for (const band of ["Needs you", "In flight", "Recently active", "Idle"]) {
    await expect(
      page.getByRole("heading", { name: band, exact: true }),
    ).toHaveCount(0);
  }
  const cards = await layout(page, "_card_");
  expect(cards.perRow).toBeGreaterThan(1);

  // …and the order the headings conveyed survives as sort order.
  const titles = await page.locator("li").allInnerTexts();
  const idx = (t: string) => titles.findIndex((x) => x.includes(t));
  expect(idx(TITLES[0])).toBeLessThan(idx(TITLES[6])); // needs_you before in_flight
  expect(idx(TITLES[6])).toBeLessThan(idx(TITLES[8])); // in_flight before idle
});

test("the LED carries the band for a screen reader, since the heading no longer does", async ({
  page,
}) => {
  await mock(page);
  await page.setViewportSize({ width: 1900, height: 1200 });
  await page.goto("/pulse");
  await expect(page.getByText("Resume PR #731 investigation")).toBeVisible();
  // Colour alone was acceptable under a "Needs you" heading. It is not, on its own.
  await expect(
    page.getByRole("img", { name: "Needs you" }).first(),
  ).toBeAttached();
  await expect(page.getByRole("img", { name: "Idle" }).first()).toBeAttached();
});

test("on a phone everything stays exactly one column", async ({ page }) => {
  await mock(page);
  // Set the width explicitly rather than relying on the project: this assertion is about the
  // CSS at phone width, and it has to hold in the desktop project too or it proves nothing
  // about a desktop browser narrowed to a phone-sized window.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/pulse");
  await expect(page.getByText("Resume PR #731 investigation")).toBeVisible();
  const cards = await layout(page, "_card_");
  const feed = await layout(page, "_act_");
  expect(cards.perRow).toBe(1);
  expect(feed.perRow).toBe(1);
});
