import { expect, test, type Page } from "@playwright/test";

import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
} from "./mission-console";

// #754 — Pulse must use the width it has.
//
// Measured on the shipped v0.17.2 build at 1900px, with the numbers this spec now pins:
// session cards 294px wide, 5 per row (fine) — activity feed rows 1490px wide, ONE per row,
// content ending around 1000px with `conf` and `Open session` marooned at the right edge.
// The queue this issue opened about was removed in #762; the feed underneath inherited its
// defect verbatim. Separately the cards rendered as four state sections, each leaving a
// partial row.
//
// #777 then merged the feed INTO the cards, and #878 replaced the card grid with the mission
// console. #948 P3 removed the last card surface — the "Sessions without a mission" view — so the
// properties are asserted where the decisions now live: a mission's thread. REMOVED with that view,
// because the UI no longer exists:
//   - the four band headings' absence and the card list's needs-you-first ORDER (there is no
//     session card list under /mission to have sections or an order);
//   - `_sessionBlock_` one-per-row at phone width (the block itself is gone).
const NOW = Math.floor(Date.now() / 1000);

/** A server-shaped mission id (`msn_` + 32 hex), so the `?m=` deep link is honoured. */
const MID = `msn_${"d754".repeat(8)}`;

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
  ["p3", "docs-site"],
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
];

const CARDS = TITLES.map((t, i) => {
  const eng = ["claude", "codex", "opencode", "gemini"][i % 4];
  const [pid, pname] = PROJECTS[i % 4];
  const id = `${eng}:${"0".repeat(7)}${i}-0000-4000-8000-00000000000${i % 10}`;
  return {
    id,
    engine: eng,
    title: t,
    cwd: `/home/u/${pname}`,
    project: { kind: "project", id: pid, name: pname, color: "#ffb000" },
    state: i < 6 ? "needs_you" : "in_flight",
    live: i >= 6,
    last_activity: NOW - i * 3600,
    intervention_required: i < 3,
    intervention_reason: i < 3 ? "waiting on a decision" : "",
    reviewed_at: NOW - 600,
    ai_summary:
      "Agent finished the edit, validated the JSON and stopped without confirming the next step.",
    synthesis: null,
    mission_id: MID,
    ...(i < 6
      ? {
          pending_action: act(i, id, t),
          state_without_action: "idle",
        }
      : {}),
  };
});

const KEYS = CARDS.map((c) => c.id);

async function mock(page: Page) {
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
        banner:
          "Six sessions are waiting on a decision; two are still working.",
        cards: CARDS,
      },
    }),
  );
  // One mission holding all eight sessions — six of them with a decision pending.
  await mockMissions(page, {
    missions: missionList([
      missionRow({
        id: MID,
        title: "Density sweep",
        session_keys: KEYS,
        needs_you: true,
      }),
    ]),
    mission: {
      ...MISSION,
      id: MID,
      title: "Density sweep",
      sessions: KEYS.map((k) => ({ session_key: k, removed_at: null })),
      events: [],
      events_next_seq: null,
    },
  });
}

/** Widths + per-row counts for a class-name prefix (CSS modules hash the suffix). */
async function layout(page: Page, prefix: string) {
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

test("at 1900px the rail is a real column BESIDE the mission's thread", async ({
  page,
}) => {
  await mock(page);
  await page.setViewportSize({ width: 1900, height: 1200 });
  await page.goto(`/mission?m=${MID}`);
  await expect(page.getByTestId("console-title")).toHaveText("Density sweep");
  await expect(page.getByText(TITLES[0])).toBeVisible();

  // DROPPED by #878: `cards.perRow > 1`. The multi-column grid it measured does not exist — the
  // console's pane is a single column by design, and the width a 1900px viewport used to spend on
  // extra card columns now goes to the rail. So the property is asserted in its new form: the
  // rail is a real column BESIDE the pane at this width, not stacked above it.
  const railBox = await page
    .getByRole("navigation", { name: /missions/i })
    .boundingBox();
  const paneBox = await page.getByTestId("pane").boundingBox();
  expect(railBox).not.toBeNull();
  expect(paneBox).not.toBeNull();
  expect(railBox!.x + railBox!.width).toBeLessThanOrEqual(paneBox!.x + 1);
});

test("what needs you carries its band for a screen reader, not just a colour", async ({
  page,
}) => {
  // Colour alone was acceptable under a "Needs you" heading. It is not, on its own. The card LED
  // this asserted went with the untracked view (#948 P3); the front door's NEEDS YOU preview is
  // the surface that now says a mission needs you, and its LED must say so too.
  await mock(page);
  await page.setViewportSize({ width: 1900, height: 1200 });
  await page.goto("/mission");
  const row = page.getByTestId("landing-needs-row");
  await expect(row).toContainText("Density sweep");
  await expect(row.getByRole("img", { name: "Needs you" })).toBeAttached();
});

test("on a phone everything stays exactly one column", async ({ page }) => {
  await mock(page);
  // Set the width explicitly rather than relying on the project: this assertion is about the
  // CSS at phone width, and it has to hold in the desktop project too or it proves nothing
  // about a desktop browser narrowed to a phone-sized window.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/mission?m=${MID}`);
  await expect(page.getByTestId("console-title")).toHaveText("Density sweep");
  await expect(page.getByText(TITLES[5])).toBeVisible();
  // One column: the six decision rows are never laid out side by side at phone width, and the
  // page does not scroll sideways.
  const feed = await layout(page, "_act_");
  expect(feed.n).toBe(6);
  expect(feed.perRow).toBe(1);
  const wide = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
  expect(wide).toBe(false);
});
