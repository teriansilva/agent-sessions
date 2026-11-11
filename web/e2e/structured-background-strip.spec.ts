import { expect, test, type Page } from "@playwright/test";

import { storeMotionChoice } from "./nightjarShell";
import { ID, mockStructuredGuidance } from "./structuredGuidanceFixture";
import { TOUCH_FLOOR } from "./touchTarget";

// #1726: the live generation's background jobs are ONE line glued above the composer, expanding
// (the shared Disclosure, grid rows) into a height-capped list that scrolls inside — never a
// transcript row, never hiding the composer or the newest row. Desktop + mobile, both themes,
// Full / Reduced / Off.

type Job = Record<string, unknown>;
const now = () => Math.floor(Date.now() / 1000);

function agentTask(i: number, extra: Job = {}): Job {
  return {
    task_id: `task-${i}`,
    state: "running",
    description: `npm run dev ${i}`,
    task_type: "local_bash",
    summary: `ready in ${800 + i} ms`,
    started_at: now() - 14 * 60 - i,
    updated_at: now() - 3,
    ...extra,
  };
}

async function session(
  page: Page,
  jobs: Job[],
  { theme = "dark", motion = "full" }: { theme?: string; motion?: "full" | "reduced" | "off" } = {},
) {
  await storeMotionChoice(page, motion);
  const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt", theme);
  fixture.state.state = "idle";
  fixture.state.active_turn = "";
  fixture.state.turns[0].state = "completed";
  fixture.state.turns = [fixture.state.turns[0]];
  Object.assign(fixture.state.native, { background_active: jobs.length > 0, background_tasks: jobs });
  await page.goto(`/s/claude-api/${ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  fixtures.set(page, fixture);
  return fixture;
}
const fixtures = new WeakMap<Page, Awaited<ReturnType<typeof mockStructuredGuidance>>>();
const mockState = async (page: Page) => fixtures.get(page)!;

const strip = (page: Page) => page.getByTestId("structured-background-work");
const toggle = (page: Page) => page.getByTestId("structured-background-strip").locator("> button");
const list = (page: Page) => page.getByTestId("structured-background-list");
const composer = (page: Page) => page.getByRole("form", { name: "Compose message" });

for (const theme of ["dark", "light"] as const) {
  test(`#1726: one collapsed 44 px line names the jobs, the lead job and its age (${theme})`, async ({ page }, testInfo) => {
    await session(page, [agentTask(1), agentTask(2)], { theme });
    await expect(strip(page)).toHaveCount(1);
    await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
    await expect(toggle(page)).toContainText("2 agent tasks running");
    await expect(toggle(page)).toContainText("npm run dev 1");
    await expect(toggle(page)).toContainText(/1[45]m/);
    await expect(toggle(page)).toContainText("Show");
    const bar = (await toggle(page).boundingBox())!;
    expect(bar.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    expect(bar.height).toBeLessThan(60); // one line
    // Collapsed: the rows are out of reach (inert, zero height), not merely scrolled away.
    const body = strip(page).locator(".nj-disclosure-body");
    await expect(body).toHaveAttribute("inert", "");
    await expect.poll(async () => (await body.boundingBox())!.height).toBeLessThan(1);
    // The LED is never the only signal: the words say it runs.
    await expect(strip(page).locator(".hud-led, [class*=led]").first()).toBeAttached();
    await page.screenshot({ path: testInfo.outputPath(`strip-collapsed-${theme}.png`) });
    await toggle(page).click();
    await expect(toggle(page)).toHaveAttribute("aria-expanded", "true");
    await expect(toggle(page)).toContainText("Hide");
    await expect(page.getByTestId("structured-background-task")).toHaveCount(2);
    await expect(page.getByTestId("structured-background-task").first()).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`strip-expanded-${theme}.png`) });
  });
}

test("#1726: with nothing running there is no strip — not an empty bar", async ({ page }) => {
  const fixture = await session(page, []);
  await expect(page.getByTestId("structured-turn")).toHaveCount(1);
  await expect(strip(page)).toHaveCount(0);
  // Jobs a generation's end cut off are #1724's transcript rows: history never keeps a strip up.
  Object.assign(fixture.state.native, {
    worker_ends: [{ worker: "old", reason: "stop", at: now() - 60, settled: true, after_turn: null,
      tasks: [{ task_id: "cut", description: "npm run dev" }] }],
  });
  fixture.state.revision++;
  await expect(strip(page)).toHaveCount(0);
  // A live job appears; the worker exits (no live generation): the strip goes with it.
  Object.assign(fixture.state.native, { background_tasks: [agentTask(1)], background_active: true });
  fixture.state.revision++;
  await expect(strip(page)).toHaveCount(1);
  fixture.state.native.worker = "";
  await expect(strip(page)).toHaveCount(0);
});

test("#1726: agent tasks and the processes the agent started are counted apart, never summed", async ({ page }) => {
  await session(page, [
    agentTask(1),
    { task_id: "pid:4821:99", state: "running", source: "process", pid: 4821, comm: "cargo",
      started_at: now() - 3600, updated_at: null },
    { task_id: "pid:4822:99", state: "running", source: "process", pid: 4822, comm: "node",
      started_at: now() - 60, updated_at: null },
  ]);
  await expect(toggle(page)).toContainText("1 agent task · 2 processes running");
  await toggle(page).click();
  const processes = page.getByTestId("structured-background-process");
  await expect(processes).toHaveCount(2);
  await expect(processes.first()).toContainText("cargo · pid 4821");
  await expect(page.getByTestId("structured-background-task")).toHaveCount(1);
});

test("#1726: many live jobs scroll inside a capped list; the composer and the newest row stay visible", async ({ page }, testInfo) => {
  await session(page, Array.from({ length: 40 }, (_, i) => agentTask(i)));
  await toggle(page).click();
  await expect(page.getByTestId("structured-background-task")).toHaveCount(40);
  const pane = (await page.getByTestId("structured-pane").boundingBox())!;
  const shown = (await list(page).boundingBox())!;
  expect(shown.height).toBeLessThanOrEqual(pane.height * 0.4 + 2);
  expect(await list(page).evaluate((el) => el.scrollHeight > el.clientHeight + 10)).toBe(true);
  // The composer is fully on screen, below the strip, and its box is still usable.
  const viewport = page.viewportSize()!;
  const form = (await composer(page).boundingBox())!;
  const bar = (await strip(page).boundingBox())!;
  expect(form.y + form.height).toBeLessThanOrEqual(viewport.height + 1);
  expect(form.y).toBeGreaterThanOrEqual(bar.y + bar.height - 1);
  await expect(page.getByRole("textbox").first()).toBeVisible();
  // The newest transcript row is still in the log's view, above the strip.
  const log = (await page.getByTestId("api-log").boundingBox())!;
  expect(log.height).toBeGreaterThan(40);
  const lastRow = (await page.getByTestId("structured-turn").last().boundingBox())!;
  expect(lastRow.y).toBeLessThan(log.y + log.height);
  await page.screenshot({ path: testInfo.outputPath("strip-many-jobs.png") });
  // Keyboard: the list is its own scroll stop; the last job is reachable.
  await list(page).focus();
  await expect(list(page)).toBeFocused();
  await page.keyboard.press("End");
  await expect.poll(() => list(page).evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
  const last = page.getByTestId("structured-background-task").last();
  await expect(last).toBeInViewport();
  // Its Details stay keyboard-reachable from inside the list.
  await last.locator("summary").focus();
  await page.keyboard.press("Enter");
  await expect(last.locator("details")).toHaveAttribute("open", "");
});

test("#1726: the strip toggle sits in the tab order between the log and the composer, and keys work it", async ({ page }) => {
  await session(page, [agentTask(1)]);
  await toggle(page).focus();
  await page.keyboard.press("Enter");
  await expect(toggle(page)).toHaveAttribute("aria-expanded", "true");
  await expect(toggle(page)).toBeFocused();
  await page.keyboard.press("Space");
  await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
  // Collapsed, the next stop after the toggle is the composer, not a hidden row.
  await page.keyboard.press("Tab");
  const focused = await page.evaluate(() => {
    const el = document.activeElement;
    return !!el && !!el.closest("form[aria-label='Compose message']");
  });
  expect(focused).toBe(true);
});

// #1724's two notices live in the strip now: visible whenever they apply, with no other live job
// too, each with its way into the Stop dialog (Keep running first), and never in the transcript.
const stopInventory = (extra: Job = {}) => ({
  worker: "worker", digest: "d".repeat(64), scan: "ok", tasks: [], processes: [], more: 0,
  observed_at: now(), ...extra,
});
const logRows = (page: Page) => page.getByTestId("api-log");

test("#1726: an unreadable process scan renders the strip by itself; its Stop asks first", async ({ page }, testInfo) => {
  await session(page, []);
  const fixture = await mockState(page);
  Object.assign(fixture.state.native, { stop_inventory: stopInventory({ scan: "unknown" }) });
  fixture.state.revision++;
  const row = strip(page).getByTestId("structured-scan-unknown");
  await expect(row).toContainText("can’t list this session’s processes — it won’t idle out");
  await expect(logRows(page).getByTestId("structured-scan-unknown")).toHaveCount(0);
  await expect(page.getByTestId("structured-background-strip")).toHaveCount(0); // no jobs: no line
  const stop = row.getByRole("button", { name: "Stop…" });
  expect((await stop.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await expect(composer(page)).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath("strip-scan-unknown.png") });
  await stop.click();
  const dialog = page.getByTestId("structured-stop-confirm");
  await expect(dialog).toHaveAttribute("data-scan", "unknown");
  await expect(dialog.getByRole("button", { name: "Keep running" })).toBeFocused();
});

for (const theme of ["dark", "light"] as const) {
  test(`#1927: a pending wakeup renders the strip by itself — the time and the reason, nothing to press (${theme})`, async ({ page }, testInfo) => {
    await session(page, [], { theme });
    const fixture = await mockState(page);
    const at = now() + 25 * 60;
    Object.assign(fixture.state.native, { wakeup: { at, reason: "watching CI on #1929" } });
    fixture.state.revision++;
    const row = strip(page).getByTestId("structured-wakeup");
    const time = await page.evaluate(
      (at) => new Date(at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      at,
    );
    await expect(row).toHaveText(`Wakes at ${time} · watching CI on #1929`);
    await expect(row.getByRole("button")).toHaveCount(0);
    await expect(logRows(page).getByTestId("structured-wakeup")).toHaveCount(0); // never a transcript row
    await expect(page.getByTestId("structured-background-strip")).toHaveCount(0); // not a job: no job line
    await expect(composer(page)).toBeInViewport();
    await page.screenshot({ path: testInfo.outputPath(`strip-wakeup-${theme}.png`) });
    // It fired (or was cancelled): the snapshot drops it, and with nothing else the strip goes.
    Object.assign(fixture.state.native, { wakeup: null });
    fixture.state.revision++;
    await expect(strip(page)).toHaveCount(0);
  });
}

test("#1726: jobs an interrupted Stop could not resume render the strip by itself; it leads to Stop everything", async ({ page }, testInfo) => {
  await session(page, []);
  const fixture = await mockState(page);
  Object.assign(fixture.state.native, { paused_unresumed: 2, stop_inventory: stopInventory() });
  fixture.state.revision++;
  const row = strip(page).getByTestId("structured-paused-unresumed");
  await expect(row).toContainText("2 jobs were paused by an interrupted Stop and could not be resumed — Stop everything ends them");
  await expect(logRows(page).getByTestId("structured-paused-unresumed")).toHaveCount(0);
  await expect(page.getByTestId("structured-background-strip")).toHaveCount(0);
  const stop = row.getByRole("button", { name: "Stop everything…" });
  expect((await stop.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await expect(composer(page)).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath("strip-paused-unresumed.png") });
  await stop.click();
  const dialog = page.getByTestId("structured-stop-confirm");
  await expect(dialog).toHaveAttribute("data-kind", "everything");
  await expect(dialog.getByRole("button", { name: "Keep running" })).toBeFocused();
});

test("#1726: the notices stay up above the job line, and while a turn runs", async ({ page }) => {
  await session(page, [agentTask(1)]);
  const fixture = await mockState(page);
  Object.assign(fixture.state.native, {
    paused_unresumed: 1, stop_inventory: stopInventory({ scan: "unknown" }),
  });
  Object.assign(fixture.state, { active_turn: fixture.state.turns[0].turn_id, state: "running" });
  fixture.state.turns[0].state = "running";
  fixture.state.revision++;
  const paused = strip(page).getByTestId("structured-paused-unresumed");
  const unknown = strip(page).getByTestId("structured-scan-unknown");
  await expect(paused).toContainText("1 job was paused");
  await expect(unknown).toBeVisible();
  await expect(toggle(page)).toContainText("1 agent task running");
  // Collapsing or opening the job line never hides them; the composer stays below all of it.
  await toggle(page).click();
  await expect(paused).toBeVisible();
  const bar = (await toggle(page).boundingBox())!;
  expect((await unknown.boundingBox())!.y).toBeLessThan(bar.y);
  expect((await composer(page).boundingBox())!.y).toBeGreaterThanOrEqual(bar.y + bar.height - 1);
});

test("#1726: a generation's cut-off work is a transcript row, never a strip row", async ({ page }) => {
  await session(page, []);
  const fixture = await mockState(page);
  Object.assign(fixture.state.native, {
    worker_ends: [{ worker: "old", reason: "stop", at: now() - 60, settled: true, after_turn: null,
      tasks: [{ task_id: "cut", description: "npm run dev" }] }],
  });
  fixture.state.revision++;
  await expect(logRows(page).getByTestId("structured-worker-end")).toContainText("npm run dev");
  await expect(strip(page)).toHaveCount(0);
});

for (const motion of ["full", "reduced", "off"] as const) {
  test(`#1726: strip expansion motion — ${motion}`, async ({ page }) => {
    await session(page, [agentTask(1)], { motion });
    const body = strip(page).locator(".nj-disclosure-body");
    const inner = strip(page).locator(".nj-disclosure-inner");
    const timing = async () => ({
      rows: await body.evaluate((el) => {
        const s = getComputedStyle(el);
        const props = s.transitionProperty.split(",").map((p) => p.trim());
        const i = props.findIndex((p) => p === "grid-template-rows" || p === "all");
        return i < 0 ? 0 : parseFloat(s.transitionDuration.split(",")[i] ?? "0");
      }),
      fade: await inner.evaluate((el) => parseFloat(getComputedStyle(el).transitionDuration)),
    });
    const t = await timing();
    if (motion === "full") {
      expect(t.rows).toBeCloseTo(0.18, 2); // grid rows 0fr → 1fr, 180 ms
      expect(t.fade).toBeCloseTo(0.18, 2);
    } else if (motion === "reduced") {
      expect(t.rows).toBe(0); // no height slide …
      expect(t.fade).toBeLessThanOrEqual(0.12); // … a ≤ 120 ms fade
      expect(t.fade).toBeGreaterThan(0);
    } else {
      expect(t.rows).toBe(0);
      expect(t.fade).toBe(0);
    }
    await toggle(page).click();
    await expect(page.getByTestId("structured-background-task")).toBeVisible();
  });
}

test("#1725: a task whose report couldn't be read isn't counted as running", async ({ page }) => {
  await session(page, [agentTask(1), agentTask(2, { state: "uncertain", description: "a lost report" })]);
  await expect(toggle(page)).toContainText("1 agent task running · 1 unconfirmed");
  await toggle(page).click();
  const row = page.getByTestId("structured-background-task").filter({ hasText: "a lost report" });
  await expect(row).toHaveAttribute("data-state", "uncertain");
  await expect(row).toContainText("Uncertain");
  await expect(row).toContainText("its requests are refused");
});

test("#1725: with only such a task, nothing says running", async ({ page }) => {
  await session(page, [agentTask(1, { state: "uncertain", description: "a lost report" })]);
  await expect(toggle(page)).toContainText("1 unconfirmed");
  await expect(toggle(page)).not.toContainText("running");
});
