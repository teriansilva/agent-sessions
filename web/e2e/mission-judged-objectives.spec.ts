/** A supervisor-judged objective, in a real browser (#1088).
 *
 * jsdom cannot say whether the evidence disclosure is a real 44px target, whether a quote carrying
 * markup stays TEXT in the rendered page, whether the panel fits a phone, or what request the
 * overrule actually sends. So these assert on the rendered page and on the REQUEST: "Not met — judge
 * again" is ONE `PATCH /objectives` carrying `{op: "reject_judgment", key, episode}` for the episode
 * the row was drawn at; the judgment threshold saves ONCE, on release, and never below its floor.
 *
 * Run on both projects (desktop + mobile), in both themes.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
  openMissionDetails,
  openMissionRail,
} from "./mission-console";
import { missionControlSettings } from "./mission-directions";

const T = 1_700_000_000;
const SRC = "transcript:claude:5f3c0000-0000-0000-0000-0000000000a1";
/** A quote carrying markup. It must render as these exact characters, never as an element. */
const HOSTILE_QUOTE = '<img src=x onerror="window.__pwned=1"> Root cause: the lock order.';

const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
  pulse: { configured: true },
  ai_review: { configured: true },
};

type Obs = Record<string, unknown>;

function verdict(over: Obs = {}, rec: Obs = {}): Obs {
  return {
    at: T - 120,
    detail: "the finding names a cause and is committed to a file the operator can read",
    value: true,
    judged: {
      met: true,
      confidence: 0.93,
      threshold: 0.9,
      evidence: [
        { source: SRC, quote: HOSTILE_QUOTE },
        { source: "diff", quote: "+## Finding\n+The reconnect storm is a lock-order race" },
      ],
      fingerprint: "f",
      checked_at: T - 120,
      ...rec,
    },
    ...over,
  };
}

const ROWS: Record<string, { title: string; state: string; observed: Obs | null; probe?: string }> =
  {
    checks: {
      title: "Checks are green",
      state: "met",
      probe: "forge_checks",
      observed: { at: T - 300, detail: "checks green", value: true },
    },
    finding: { title: "A finding is written down", state: "met", observed: verdict() },
    stale: {
      title: "The fix is described",
      state: "met",
      observed: verdict({ stale: true, reason: "the session output changed since the judgment" }),
    },
    below: {
      title: "Root cause is confirmed with a reproduction",
      state: "pending",
      observed: verdict({ value: false }, { confidence: 0.62 }),
    },
    noend: {
      title: "Done as you instructed",
      state: "pending",
      observed: {
        at: T - 60,
        stale: true,
        reason: "cannot be judged — no AI endpoint is configured",
        detail: "cannot be judged — no AI endpoint is configured",
        judged: { attempted_fp: "f", transient: true },
      },
    },
  };

interface Server {
  patches: { ops: Record<string, unknown>[] }[];
  rows: typeof ROWS;
}

function objectiveRows(s: Server) {
  return Object.entries(s.rows).map(([key, r], i) => ({
    mission_id: "msn_1",
    key,
    ord: i,
    title: r.title,
    probe: r.probe ?? "supervisor_judged",
    probe_args: null,
    gate: true,
    state: r.state,
    met_at: r.state === "met" ? T - 120 : null,
    observed: r.observed,
    source: "playbook",
    judge_rejected: key === "finding" && r.state === "pending",
  }));
}

function reading(key: string, title: string, state: string) {
  return {
    key,
    title,
    gate: true,
    state,
    met: state === "met",
    current: true,
    episode: 2,
    stood_down: false,
    awaiting_answer: false,
    spent: 0,
    remaining: 3,
    may_nudge: false,
    unreadable: false,
    indeterminate: false,
    live: 0,
    terminal: false,
    why_not: "",
  };
}

async function setup(page: Page, theme: "dark" | "light"): Promise<Server> {
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  const server: Server = { patches: [], rows: JSON.parse(JSON.stringify(ROWS)) };
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
  await page.route("**/api/projects**", (r) => r.fulfill({ json: { projects: [] } }));
  const supervisor = {
    objectives: Object.entries(ROWS).map(([k, r]) => reading(k, r.title, r.state)),
    likely_done: false,
    unmet_gates: 3,
    gates: 5,
    held_sessions: 1,
    no_session: false,
    checked_at: T - 30,
  };
  await mockMissions(page, {
    missions: missionList([missionRow({ session_keys: ["claude:aaa"], state: "running" })]),
    mission: {
      ...MISSION,
      state: "running",
      sessions: [{ session_key: "claude:aaa", removed_at: null }],
      events: [],
      events_next_seq: null,
      supervisor,
    },
  });
  await page.route("**/api/missions/*/objectives", async (r) => {
    if (r.request().method() === "PATCH") {
      const body = r.request().postDataJSON() as { ops: Record<string, unknown>[] };
      server.patches.push(body);
      for (const op of body.ops)
        if (op.op === "reject_judgment" && op.key === "finding") {
          server.rows.finding = {
            ...server.rows.finding,
            state: "pending",
            observed: verdict({ value: false, rejected_at: T }),
          };
        }
      return r.fulfill({ json: { objectives: objectiveRows(server) } });
    }
    return r.fulfill({ json: { objectives: objectiveRows(server) } });
  });
  return server;
}

async function openObjectives(page: Page) {
  await page.goto("/mission");
  await expect(page.getByTestId("mission-console")).toBeVisible();
  await openMissionRail(page);
  await page.locator('[data-testid="rail-mission"]:visible').first().click();
  if (await page.getByRole("dialog").count()) {
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
  }
  await expect(page.getByTestId("mission-state")).toBeVisible();
  await openMissionDetails(page, "objectives");
  await expect(row(page, "finding")).toBeVisible();
}

function row(page: Page, key: string) {
  return page.getByTestId("objectives").locator(`[data-testid="objective"][data-key="${key}"]`);
}

for (const theme of ["dark", "light"] as const) {
  test.describe(`${theme} theme`, () => {
    test("a judged row reads 'judged met', and its evidence opens as PLAIN TEXT", async ({
      page,
    }) => {
      await setup(page, theme);
      await openObjectives(page);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);

      // A judgment is labelled as one; an observation as the other.
      const finding = row(page, "finding");
      await expect(finding.getByTestId("objective-state")).toHaveText("judged met (0.93)");
      await expect(finding.getByTestId("objective-tag")).toHaveText("judged");
      await expect(row(page, "checks").getByTestId("objective-tag")).toHaveText("observed");

      const toggle = finding.getByTestId("objective-evidence-toggle");
      await expect(toggle).toHaveAttribute("aria-expanded", "false");
      const tb = (await toggle.boundingBox())!;
      expect(tb.height).toBeGreaterThanOrEqual(44);
      await toggle.click();
      await expect(toggle).toHaveAttribute("aria-expanded", "true");
      const panel = finding.getByTestId("objective-evidence");
      await expect(panel).toBeVisible();
      const quotes = panel.getByTestId("objective-evidence-quote");
      await expect(quotes).toHaveCount(2);
      // THE MARKUP IS TEXT: the exact characters are on the page, no element was built, and the
      // handler never ran.
      await expect(quotes.first()).toHaveText(HOSTILE_QUOTE);
      await expect(panel.locator("img")).toHaveCount(0);
      expect(await page.evaluate(() => (window as { __pwned?: number }).__pwned)).toBeUndefined();
      await expect(panel).toContainText("transcript · claude:5f3c…a1");
      await expect(panel).toContainText("Why: the finding names a cause");
      await expect(panel).toContainText(/Holds while the session is unchanged since/);

      // The panel fits the viewport — no sideways scroll on a phone.
      const vw = page.viewportSize()!.width;
      const pb = (await panel.boundingBox())!;
      expect(pb.x + pb.width).toBeLessThanOrEqual(vw + 0.5);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
        vw,
      );
      // Quote text is readable against its own ground in THIS theme.
      const [fg, bg] = await quotes.first().evaluate((el) => {
        const s = getComputedStyle(el);
        return [s.color, s.backgroundColor];
      });
      expect(fg).not.toBe(bg);
    });

    test("the states: stale, below the threshold, no endpoint", async ({ page }) => {
      await setup(page, theme);
      await openObjectives(page);
      const stale = row(page, "stale");
      await expect(stale.getByTestId("objective-judged-stale")).toContainText("stale");
      await expect(stale).toContainText(/does not count toward completion until then/);
      await expect(stale.getByTestId("objective-evidence-toggle")).toHaveText(
        /^Show evidence from /,
      );

      const below = row(page, "below");
      await expect(below.getByTestId("objective-state")).toHaveText("judged not yet (0.62)");
      await below.getByTestId("objective-evidence-toggle").click();
      await expect(below.getByTestId("objective-evidence")).toContainText(/below your 0\.90/i);
      await expect(below.getByTestId("objective-reject-judgment")).toHaveCount(0);

      const noend = row(page, "noend");
      await expect(noend.getByTestId("objective-judged-unknown")).toContainText(
        "Cannot be judged — no AI endpoint is configured",
      );
    });
  });
}

test("'Not met — judge again' sends ONE reject_judgment for the rendered episode", async ({
  page,
}) => {
  const server = await setup(page, "dark");
  await openObjectives(page);
  const finding = row(page, "finding");
  await finding.getByTestId("objective-evidence-toggle").click();
  const reject = finding.getByTestId("objective-reject-judgment");
  await expect(reject).toBeVisible();
  const rb = (await reject.boundingBox())!;
  expect(rb.height).toBeGreaterThanOrEqual(44);
  await reject.click();
  await expect.poll(() => server.patches.length).toBe(1);
  expect(server.patches[0]).toEqual({
    ops: [{ op: "reject_judgment", key: "finding", episode: 2 }],
  });
  // The row re-reads what the server holds: no longer met, and it says the operator rejected it.
  await expect(finding.getByTestId("objective-judged-rejected")).toBeVisible();
  await expect(finding.getByTestId("objective-reject-judgment")).toHaveCount(0);
  // Nothing further arrives.
  await page.waitForTimeout(300);
  expect(server.patches.length).toBe(1);
});

test("the judgment threshold saves ONCE, on release, and cannot go under its floor", async ({
  page,
}) => {
  const saves = await missionControlSettings(page, {
    judge_confidence_min: 0.9,
    judge_confidence_floor: 0.9,
    judge_confidence_max: 1,
  });
  const slider = page.getByTestId("judge-threshold");
  await expect(slider).toBeVisible();
  await expect(slider).toHaveAttribute("min", "0.9");
  await expect(slider).toHaveAttribute("max", "1");
  await expect(page.getByTestId("judge-threshold-value")).toHaveText("0.90");
  await expect(page.getByTestId("orchestrator-judge")).toContainText(
    /0\.90 is the floor and cannot be lowered/,
  );
  // A real drag across the track: many `input` events, ONE save on release.
  await slider.scrollIntoViewIfNeeded();
  const b = (await slider.boundingBox())!;
  const y = b.y + b.height / 2;
  await page.mouse.move(b.x + 4, y);
  await page.mouse.down();
  for (let i = 1; i <= 10; i++) await page.mouse.move(b.x + 4 + ((b.width - 8) * i) / 20, y);
  await page.mouse.up();
  await expect.poll(() => saves.filter((s) => "judge_confidence_min" in s).length).toBe(1);
  const v = saves.find((s) => "judge_confidence_min" in s)!.judge_confidence_min as number;
  expect(v).toBeGreaterThan(0.9);
  expect(v).toBeLessThanOrEqual(1);
  await page.waitForTimeout(300);
  expect(saves.filter((s) => "judge_confidence_min" in s).length).toBe(1);
});
