/** The motion modes in a real browser (#1531, docs/design.md v2 §8), desktop and mobile.
 *
 *  CSS checks alone cannot prove "Off means nothing moves": the button glitch runs a timer, the
 *  HQ portrait a video, and any requestAnimationFrame loop would run too. So these specs count those
 *  callbacks with a shim installed before the app boots, read `document.getAnimations()`, and
 *  switch modes LIVE (the gallery's own Motion control), on a device that does NOT ask to reduce
 *  motion — the case where only the setting can stop things. The atmosphere is CSS only (#1531:
 *  the #211 trail canvas and its rAF loop are retired), so "alive" means its loops are running. */
import { expect, test, type Page } from "@playwright/test";

import { DASHBOARD_PATH, NIGHTJAR_GALLERY_PATH } from "../src/lib/routes";
import { MISSION, missionList, missionRow, mockMissions, openMissionRail } from "./mission-console";
import { expectIdleBudget, mockNightjarShell, storeMotionChoice } from "./nightjarShell";
import {
  URL_PATH as API_SESSION_PATH,
  addTurn,
  complete,
  flush,
  gap as apiGap,
  mockStreamingSession,
  operatorTurn,
  paragraphs,
} from "./structuredStream";

declare global {
  interface Window {
    __nj: { raf: number; glitchTimers: number; firstFrame?: { motion?: string; anims: number } };
  }
}

/** Count rAF callbacks and glitch-range timers (7–23 s: the ambient glitch is the only timer in
 *  that range), and record the first painted frame's motion state. Installed before any app code. */
async function installShim(page: Page) {
  await page.addInitScript(() => {
    const nj = (window.__nj = { raf: 0, glitchTimers: 0 } as Window["__nj"]);
    const raf = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = (cb: FrameRequestCallback) =>
      raf((t) => {
        nj.raf++;
        cb(t);
      });
    const st = window.setTimeout.bind(window);
    window.setTimeout = ((fn: TimerHandler, ms?: number, ...a: unknown[]) => {
      if (typeof ms === "number" && ms >= 7000 && ms <= 23000) nj.glitchTimers++;
      return st(fn, ms, ...a);
    }) as typeof window.setTimeout;
    raf(() => {
      nj.firstFrame = {
        motion: document.documentElement.dataset.motion,
        anims: document.getAnimations().length,
      };
    });
  });
}

async function counters(page: Page) {
  return page.evaluate(() => ({ ...window.__nj }));
}

/** rAF callbacks the page runs over `ms`, after a settle. */
async function rafOver(page: Page, ms: number): Promise<number> {
  const a = (await counters(page)).raf;
  await page.waitForTimeout(ms);
  return (await counters(page)).raf - a;
}

/** The atmosphere's infinite CSS loops that are running right now (scan drift, sweep, …). */
async function atmoLoops(page: Page): Promise<number> {
  return page.evaluate(
    () =>
      document
        .getAnimations()
        .filter((a) => a.playState === "running" && a.effect?.getComputedTiming().iterations === Infinity)
        .filter((a) => (a.effect as KeyframeEffect).target?.closest?.(".nj-atmo")).length,
  );
}

async function setGalleryMotion(page: Page, label: "Device" | "Full" | "Reduced" | "Off") {
  await page.getByTestId("gallery-motion").getByRole("radio", { name: label }).click();
}

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await mockNightjarShell(page);
  await installShim(page);
});

for (const theme of ["dark", "light"] as const) {
  test(`the backdrop is ONE atmosphere: hex grid, no trail canvas, no square grid (${theme}) (#1531)`, async ({ page }) => {
    await storeMotionChoice(page, "full");
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    await page.goto(DASHBOARD_PATH);
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await expect(page.getByTestId("nj-atmo")).toBeAttached();
    // The #211 data-flow canvas painted drifting trails over a 48 px square grid; NIGHTJAR's
    // atmosphere replaced it, and both had kept painting, one over the other.
    await expect.soft(page.locator("canvas")).toHaveCount(0);
    const grids = await page.evaluate(() =>
      [document.documentElement, document.body, ...document.querySelectorAll(".nj-atmo, .nj-atmo *")]
        .map((el) => getComputedStyle(el))
        .filter((st) => /48px 48px/.test(st.backgroundSize)).length,
    );
    expect.soft(grids, "no square 48 px grid behind the app").toBe(0);
    const hex = await page.evaluate(() => {
      const st = getComputedStyle(document.querySelector(".nj-atmo .nj-hex")!);
      return { display: st.display, mask: st.webkitMaskImage || st.maskImage };
    });
    expect(hex.display).not.toBe("none");
    expect(hex.mask).toContain("svg");
  });
}

test("Off stored: the first frame already knows it, and nothing moves (#1531)", async ({ page }) => {
  await storeMotionChoice(page, "off");
  await page.goto(DASHBOARD_PATH);
  await expect(page.getByTestId("nj-atmo")).toBeAttached();
  const first = (await counters(page)).firstFrame;
  expect(first?.motion, "data-motion is set before the first frame").toBe("off");
  expect(first?.anims).toBe(0);

  await page.waitForTimeout(800); // settle: the boot reads land
  expect(await rafOver(page, 1500), "no rAF loop in Off").toBe(0);
  expect(await page.evaluate(() => document.getAnimations().length)).toBe(0);
  const transitions = await page.evaluate(() =>
    [...document.querySelectorAll<HTMLElement>("*")].filter((el) =>
      getComputedStyle(el).transitionDuration.split(",").some((d) => parseFloat(d) > 0),
    ).length,
  );
  expect(transitions, "every transition is 0s in Off").toBe(0);
  expect((await counters(page)).glitchTimers, "the glitch timer is never armed").toBe(0);
  // The HQ portrait is its poster, never a video.
  expect(await page.locator("video").count()).toBe(0);
});

test("Reduced: no infinite animation, no video, no rAF loop (#1531)", async ({ page }) => {
  await storeMotionChoice(page, "reduced");
  await page.goto(DASHBOARD_PATH);
  await expect(page.getByTestId("nj-atmo")).toBeAttached();
  await page.waitForTimeout(800);
  const infinite = await page.evaluate(
    () => document.getAnimations().filter((a) => a.effect?.getComputedTiming().iterations === Infinity).length,
  );
  expect(infinite).toBe(0);
  expect(await page.locator("video").count()).toBe(0);
  expect(await rafOver(page, 1200)).toBe(0);
});

test("Match device on a reduce-motion device is Reduced (#1531)", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(DASHBOARD_PATH);
  await expect(page.locator("html")).toHaveAttribute("data-motion", "reduced");
});

test("a live switch Full → Off → Full tears the loops down and restores them (#1531)", async ({ page }, info) => {
  await storeMotionChoice(page, "full");
  await page.goto(NIGHTJAR_GALLERY_PATH);
  await expect(page.getByTestId("nightjar-gallery")).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-motion", "full");
  // The atmosphere's loops run on dark in Full (desktop and phone alike).
  expect(await atmoLoops(page), "the atmosphere is alive").toBeGreaterThan(0);
  expect((await counters(page)).glitchTimers, "the glitch timer is armed in Full").toBeGreaterThan(0);
  const loops = await page.evaluate(
    () => document.getAnimations().filter((a) => a.effect?.getComputedTiming().iterations === Infinity).length,
  );
  expect(loops, "the catalog's ambient loops run in Full").toBeGreaterThan(3);

  await setGalleryMotion(page, "Off");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "off");
  await page.waitForTimeout(300);
  expect(await rafOver(page, 1000)).toBe(0);
  expect(await page.evaluate(() => document.getAnimations().length)).toBe(0);
  const armedBefore = (await counters(page)).glitchTimers;

  await setGalleryMotion(page, "Full");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "full");
  expect(await atmoLoops(page), "the atmosphere is alive again").toBeGreaterThan(0);
  expect((await counters(page)).glitchTimers, "the glitch timer is re-armed").toBeGreaterThan(armedBefore);
  info.annotations.push({ type: "motion", description: "Full → Off → Full verified" });
});

test("a hidden tab pauses the ambient loops and a visible one resumes them (#1531)", async ({ page }) => {
  await storeMotionChoice(page, "full");
  await page.goto(NIGHTJAR_GALLERY_PATH);
  await expect(page.getByTestId("nightjar-gallery")).toBeVisible();
  expect(await atmoLoops(page), "the atmosphere is alive").toBeGreaterThan(0);

  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(page.locator("html")).toHaveAttribute("data-motion-paused", "hidden");
  await page.waitForTimeout(200);
  expect(await rafOver(page, 800)).toBe(0);
  expect(await atmoLoops(page), "the atmosphere's loops are paused").toBe(0);

  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(page.locator("html")).not.toHaveAttribute("data-motion-paused", /.+/);
  expect(await atmoLoops(page), "the atmosphere is alive again").toBeGreaterThan(0);
});

test("ten idle minutes stop the ambient loops; input resumes them (#1531)", async ({ page }) => {
  await page.clock.install();
  await storeMotionChoice(page, "full");
  await page.goto(NIGHTJAR_GALLERY_PATH);
  await expect(page.getByTestId("nightjar-gallery")).toBeVisible();
  await page.clock.fastForward("10:05");
  await expect(page.locator("html")).toHaveAttribute("data-motion-paused", "idle");
  await page.mouse.move(40, 40);
  await page.mouse.move(60, 60);
  await expect(page.locator("html")).not.toHaveAttribute("data-motion-paused", /.+/);
});

test("streamed text is in the DOM as it arrives and moves nothing above it (#1531)", async ({ page }) => {
  await storeMotionChoice(page, "full");
  await page.goto(NIGHTJAR_GALLERY_PATH);
  const stream = page.getByTestId("g-stream");
  await stream.scrollIntoViewIfNeeded();
  const above = await page.getByTestId("g-panel-default").boundingBox();
  await page.getByRole("button", { name: "Stream an answer" }).click();
  // The first words are there while the stream is still open (the caret rides the end): text is
  // never held back for an animation.
  await expect(stream.locator(".nj-caret")).toBeAttached();
  await expect(stream).toContainText("Two things");
  await expect(stream).toContainText("green again", { timeout: 10_000 });
  await expect(stream.locator(".nj-caret")).toHaveCount(0);
  expect((await page.getByTestId("g-panel-default").boundingBox())?.y).toBe(above?.y);
});

test("the dashboard idles cheaply in Full (< 2% busy on an idle host; recalcs and layouts per second everywhere) (#1531)", async ({ page, browserName }, info) => {
  test.skip(browserName !== "chromium", "CDP metrics");
  test.skip(info.project.name !== "desktop", "one measurement is enough");
  await storeMotionChoice(page, "full");
  await page.goto(DASHBOARD_PATH);
  await expect(page.getByTestId("nj-atmo")).toBeAttached();
  await page.waitForTimeout(1500);
  await expectIdleBudget(page, "dashboard", info);
});

/** A RUNNING mission console with something in every part (#1573): a thread of turns and state
 *  changes, judged and open objectives, two held sessions with a live Now strip. At 1440 the details
 *  column (Context, Objectives) stands beside the thread. */
async function mockRunningConsole(page: Page) {
  const at = 1_700_000_000;
  const keys = ["claude:7c1e1a00-5b2d-4c8e-9f31-0a6d2e8b4001", "codex:7c1e2a00-5b2d-4c8e-9f31-0a6d2e8b4002"];
  const ev = (seq: number, kind: string, text: string | null, meta: Record<string, unknown> | null = null) => ({
    seq, mission_id: "msn_1", at: at + seq * 60, kind, session_key: null, action_id: null, text, meta, settlement: null,
  });
  const objectives = [
    ["limit", "Over-quota keys get 429 + Retry-After", "met"],
    ["load", "Load test holds 2k rps with p95 under 50 ms", "met"],
    ["webhooks", "Billing webhooks write v2 events on staging", "pending"],
    ["docs", "Limits documented for partners", "pending"],
  ].map(([key, title, state], ord) => ({
    mission_id: "msn_1", key, ord, title, probe: "supervisor_judged", probe_args: null, gate: true, state,
    met_at: state === "met" ? at : null, source: "playbook",
    observed: state === "met"
      ? { at, value: true, judged: { met: true, confidence: 0.94, threshold: 0.9, fingerprint: "f", checked_at: at, evidence: [{ source: `transcript:${keys[0]}`, quote: "✓ 12 passed" }] } }
      : null,
  }));
  await mockMissions(page, {
    missions: missionList([missionRow({ session_keys: keys })]),
    mission: {
      ...MISSION,
      sessions: keys.map((k) => ({ mission_id: "msn_1", session_key: k, role: null, added_at: at, removed_at: null })),
      objectives,
      objectives_state: "done",
      events: [
        ev(1, "operator_msg", "Harden the public ingest API before the partner launch."),
        ev(2, "assistant_msg", "Plan ready. Claude takes the limiter; Codex migrates the webhooks."),
        ev(3, "state", "Mission launched — 2 sessions", { from: "planned", to: "running" }),
        ev(4, "assistant_msg", "Limiter is in and its tests are green. Objective 1 met."),
        ev(5, "assistant_msg", "Load test held 2,031 rps with p95 at 41.7 ms. Objective 2 met."),
        ev(6, "operator_msg", "Good. Codex can run the migration on staging once the dry run is clean."),
      ],
      events_next_seq: null,
    },
    objectives: { objectives },
    context: { id: "msn_1", project_id: "agent-sessions", cwd: "/repo", sessions: keys.map((k) => ({ mission_id: "msn_1", session_key: k, role: null, added_at: at, removed_at: null })), git: null, git_error: null },
    now: {
      sessions: [
        { session_key: keys[0], status: "producing", seconds_since_output: 2, prompt_class: null, recap_age_s: 60, recap_older_than_output: false },
        { session_key: keys[1], status: "at_prompt", seconds_since_output: 210, prompt_class: "confirm", recap_age_s: 120, recap_older_than_output: false },
      ],
      checked_at: at,
    },
  });
}

test("a running mission console idles cheaply in Full (< 2% busy on an idle host; recalcs and layouts per second everywhere) (#1573)", async ({ page, browserName }, info) => {
  test.skip(browserName !== "chromium", "CDP metrics");
  test.skip(info.project.name !== "desktop", "one measurement is enough");
  await mockRunningConsole(page);
  await storeMotionChoice(page, "full");
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/mission");
  await page.getByRole("button", { name: /Kimi transcript adapter/ }).click();
  await expect(page.getByTestId("mission-details")).toBeVisible();
  await expect(page.getByTestId("objectives")).toBeVisible();
  await expect(page.getByTestId("thread-event")).toHaveCount(6);
  await page.waitForTimeout(1500);
  await expectIdleBudget(page, "mission console", info);
  // Nothing on the console's own surface loops: the lights are steady, only the atmosphere moves.
  const loops = await page.evaluate(
    () =>
      document
        .getAnimations()
        .filter((x) => x.effect?.getComputedTiming().iterations === Infinity)
        .filter((x) => (x.effect as KeyframeEffect).target?.closest?.('[data-testid="mission-console"]')).length,
  );
  expect(loops, "no infinite animation inside the console").toBe(0);
});

test("the mission console in Reduced and Off: no loop, no rAF, nothing moving in Off (#1573)", async ({ page }) => {
  await mockRunningConsole(page);
  for (const mode of ["reduced", "off"] as const) {
    await storeMotionChoice(page, mode);
    await page.goto("/mission");
    await openMissionRail(page); // a phone's rail is a drawer; a no-op where it is docked
    await page.getByRole("button", { name: /Kimi transcript adapter/ }).click();
    await expect(page.getByTestId("thread-event").first()).toBeVisible();
    await page.waitForTimeout(800);
    expect(await rafOver(page, 1200), `no rAF loop in ${mode}`).toBe(0);
    const infinite = await page.evaluate(
      () => document.getAnimations().filter((x) => x.effect?.getComputedTiming().iterations === Infinity).length,
    );
    expect(infinite, `no infinite animation in ${mode}`).toBe(0);
    if (mode === "off") expect(await page.evaluate(() => document.getAnimations().length)).toBe(0);
  }
});

// ---- An API session (#1727): streaming, arrival and the follow-scroll in each mode ----------

const streamingTurns = () => [
  operatorTurn({
    turn_id: "22222222-2222-4333-8444-555555555555",
    operation_id: "22222222-2222-4333-8444-555555555555",
    state: "completed",
    text: "First question",
    reply: paragraphs(40),
    stream: undefined,
  }),
  operatorTurn({ reply: "Starting" }),
];

test("an API session in Off: a streamed reply and an arriving row move nothing; the follow lands at once (#1727)", async ({ page }) => {
  await storeMotionChoice(page, "off");
  const s = await mockStreamingSession(page, streamingTurns());
  await page.goto(API_SESSION_PATH);
  await expect(page.getByText("Starting", { exact: false })).toBeAttached();
  await expect.poll(() => apiGap(page)).toBeLessThanOrEqual(3);
  flush(s, `\n\n${paragraphs(8, 41)}`);
  addTurn(s, operatorTurn({ turn_id: "44444444-2222-4333-8444-555555555555", operation_id: "44444444-2222-4333-8444-555555555555", state: "queued", text: "Arrives now", reply: "", stream: undefined }));
  await expect(page.getByText("Arrives now", { exact: true })).toBeAttached();
  await expect(page.getByText("Progress paragraph 48.", { exact: true })).toBeAttached();
  // Off: no word fades, the row has no animation, and the scroll is already at the bottom.
  expect(await apiGap(page)).toBeLessThanOrEqual(3);
  await expect(page.getByTestId("api-log").locator(".nj-word")).toHaveCount(0);
  expect(await page.evaluate(() => document.getAnimations().length)).toBe(0);
  expect(await rafOver(page, 1200), "no rAF loop in Off").toBe(0);
});

test("an API session in Reduced: an arriving row is a 120 ms fade; words do not fade; the caret is steady (#1727)", async ({ page }) => {
  await storeMotionChoice(page, "reduced");
  const s = await mockStreamingSession(page, streamingTurns());
  await page.goto(API_SESSION_PATH);
  await expect(page.getByText("Starting", { exact: false })).toBeAttached();
  flush(s, " the first step");
  await expect(page.getByText("Starting the first step", { exact: false })).toBeAttached();
  addTurn(s, operatorTurn({ turn_id: "44444444-2222-4333-8444-555555555555", operation_id: "44444444-2222-4333-8444-555555555555", state: "queued", text: "Arrives now", reply: "", stream: undefined }));
  const row = page.getByTestId("structured-turn").filter({ hasText: "Arrives now" });
  await expect(row).toHaveClass(/nj-arrive/);
  const anims = await row.evaluate((el) =>
    el.getAnimations().map((a) => ({
      name: (a as CSSAnimation).animationName,
      ms: Number(a.effect?.getComputedTiming().duration),
    })),
  );
  expect(anims).toEqual([{ name: "nj-fade-in", ms: 120 }]);
  await expect(page.getByTestId("api-log").locator(".nj-word")).toHaveCount(0);
  const caret = page.getByTestId("api-log").locator(".nj-caret");
  await expect(caret).toHaveCount(1);
  expect(await caret.evaluate((el) => el.getAnimations().length)).toBe(0);
  expect(await rafOver(page, 1200), "no rAF loop in Reduced").toBe(0);
});

test("an idle API session idles cheaply in Full; a finished stream leaves no caret and no loop (#1727)", async ({ page, browserName }, info) => {
  test.skip(browserName !== "chromium", "CDP metrics");
  test.skip(info.project.name !== "desktop", "one measurement is enough");
  await storeMotionChoice(page, "full");
  const s = await mockStreamingSession(page, streamingTurns());
  await page.goto(API_SESSION_PATH);
  await expect(page.getByText("Starting", { exact: false })).toBeAttached();
  flush(s, " and done.");
  await expect(page.getByText("Starting and done.", { exact: false })).toBeAttached();
  complete(s);
  await expect(page.getByTestId("structured-turn").last()).toHaveAttribute("data-state", "completed");
  await expect(page.getByTestId("api-log").locator(".nj-caret")).toHaveCount(0);
  const loops = await page.evaluate(
    () =>
      document
        .getAnimations()
        .filter((x) => x.effect?.getComputedTiming().iterations === Infinity)
        .filter((x) => (x.effect as KeyframeEffect).target?.closest?.('[data-testid="api-log"]')).length,
  );
  expect(loops, "no infinite animation inside the API log").toBe(0);
  await page.waitForTimeout(1500);
  await expectIdleBudget(page, "API session", info);
});
