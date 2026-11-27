import { expect, type Page, test } from "@playwright/test";

import { automationPath, AUTOMATIONS_PATH, SPECIALISTS_PATH } from "../src/lib/routes";
import { settingsPath } from "../src/routes/settingsTabs";
import { automation, mockAutomations } from "./automations";
import { commonMocks } from "./mission-directions";
import { animationsSettled } from "./nightjarShell";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

// The automation queue (#2422), in a real browser on the desktop AND phone projects: capacity kept
// for automations on the two limits (Library → Specialists' pool line, Settings → Session limit),
// the Automations list's queue line, and a queued run's row, notice and timeline. What breaks here
// is layout (two fields side by side on a phone, a control under 44 px) and the request the page
// actually sends (both limits in ONE prefs body; a reserve above the limit never sent).

type Json = Record<string, unknown>;

async function shot(page: Page, name: string) {
  await animationsSettled(page);
  await page.screenshot({ path: test.info().outputPath(`2422-${name}-${test.info().project.name}.png`), fullPage: true });
}

// ---- Library → Specialists: the pool line -------------------------------------------------------

async function pool(page: Page, start: { limit: number; reserve: number }) {
  const prefs: Json[] = [];
  let { limit, reserve } = start;
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await commonMocks(page);
  await mockRoster(page);
  await page.route("**/api/library/skills", (r) => r.fulfill({ json: { skills: [] } }));
  await page.route(/\/api\/specialists$/, (r) =>
    r.fulfill({
      json: {
        specialists: [],
        templates: [],
        max_instances: limit,
        live_total: 2,
        reserved_automations: reserve,
        reserved_effective: Math.min(reserve, limit),
      },
    }),
  );
  await page.route("**/api/prefs", (r) => {
    if (r.request().method() !== "POST") return r.fulfill({ json: {} });
    const body = r.request().postDataJSON() as Json;
    prefs.push(body);
    const nextLimit = Number(body.pool_max_instances ?? limit);
    const nextReserve = Number(body.pool_reserved_automations ?? reserve);
    // The server's check: a reserve above the limit in force after the save is a 422.
    if (nextReserve > nextLimit)
      return r.fulfill({ status: 422, json: { detail: "pool.reserved_automations must not exceed pool.max_instances" } });
    limit = nextLimit;
    reserve = nextReserve;
    return r.fulfill({ json: { pool_max_instances: limit, pool_reserved_automations: reserve } });
  });
  await page.goto(SPECIALISTS_PATH);
  const line = page.getByTestId("pool-capacity");
  await expect(line).toContainText(`2 of ${limit} instances running`);
  return { line, prefs };
}

test("the pool keeps slots for automations, says what is left, and saves both in one request", async ({ page }) => {
  const { line, prefs } = await pool(page, { limit: 6, reserve: 1 });
  const reserve = line.getByRole("spinbutton", { name: "Reserved for automations" });
  await expect(reserve).toHaveValue("1");
  expect((await reserve.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  const help = line.getByTestId("pool-reserve-help");
  await expect(help).toHaveText("Other starts can use 5 of 6; automations can use any free slot.");
  const save = line.getByRole("button", { name: "Save limits" });
  expect((await save.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await expect(save).toBeDisabled();
  await shot(page, "pool");

  // A reserve above the limit is refused here, with the reason, and never sent.
  await reserve.fill("7");
  await expect(line).toContainText("The reserve can't be more than the pool limit (6).");
  await expect(save).toBeDisabled();
  await expect(help).toHaveCount(0);
  await shot(page, "pool-refused");

  await reserve.fill("2");
  await expect(help).toHaveText("Other starts can use 4 of 6; automations can use any free slot.");
  await save.click();
  await expect(save).toBeDisabled();
  expect(prefs).toEqual([{ pool_max_instances: 6, pool_reserved_automations: 2 }]);
});

test("a pool reserved whole says only automations can start specialists", async ({ page }) => {
  const { line } = await pool(page, { limit: 1, reserve: 1 });
  await expect(line.getByTestId("pool-reserve-help")).toHaveText(
    "Only automations can start here: the whole pool of 1 is reserved for them.",
  );
  await shot(page, "pool-automations-only");
});

// ---- Settings → Sessions and memory → Session limit ---------------------------------------------

function resources(max: number, reserve: number): Json {
  const values = { console_tasks: 4096, api_tasks: 4096, library_threads: 8, api_memory_gib: 8, max_sessions: max, session_reserved_automations: reserve };
  return {
    settings: {
      values,
      recommended: { ...values, max_sessions: 6, session_reserved_automations: 1 },
      bounds: {
        console_tasks: { min: 256, max: 16384 },
        api_tasks: { min: 256, max: 16384 },
        library_threads: { min: 1, max: 64 },
        api_memory_gib: { min: 1, max: 1024 },
        max_sessions: { min: 1, max: 64 },
        session_reserved_automations: { min: 1, max: 64 },
      },
      sources: Object.fromEntries(Object.keys(values).map((k) => [k, "recommended"])),
      reserved_effective: Math.min(reserve, max),
      console_tasks_max: "4096",
      capacity: { memory_bytes: 10 * 1024 ** 3, cpus: 4, reserved_bytes: 4 * 1024 ** 3, by_memory: 6, by_cpu: 24, limited_by: "memory", recommended: 6 },
      running: 4,
      library_overrides: [],
      notice: null,
    },
    usage: { observed_at: 1, console_containment: "verified", truncated: false, error: null, groups: [] },
  };
}

async function sessionLimit(page: Page, max: number, reserve: number) {
  const writes: Json[] = [];
  const data = resources(max, reserve);
  await commonMocks(page);
  await page.route("**/api/system/resources*", (r) => {
    if (r.request().method() === "POST") {
      const patch = r.request().postDataJSON() as Json;
      writes.push(patch);
      const settings = data.settings as Json;
      const values = { ...(settings.values as Json), ...patch };
      if (Number(values.session_reserved_automations) > Number(values.max_sessions))
        return r.fulfill({ status: 422, json: { detail: "session_reserved_automations must not exceed max_sessions" } });
      settings.values = values;
      settings.reserved_effective = Math.min(Number(values.session_reserved_automations), Number(values.max_sessions));
      return r.fulfill({ json: { settings } });
    }
    return r.fulfill({ json: data });
  });
  await page.goto(settingsPath("resources"));
  const card = page.getByTestId("session-limit-card");
  await expect(card).toContainText("4 running");
  return { card, writes };
}

test("the session limit keeps slots for automations beside it, and refuses a reserve above it", async ({ page }) => {
  const { card, writes } = await sessionLimit(page, 6, 1);
  await expect(card).toContainText("an automation step waits in a queue for the next free slot instead");
  const limit = card.getByLabel("Maximum running sessions");
  const reserve = card.getByLabel("Reserved for automations");
  await expect(reserve).toHaveValue("1");
  expect((await reserve.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await expect(card.getByTestId("session-reserve-help")).toHaveText(
    "Other starts can use 5 of 6; automations can use any free slot. At least 1.",
  );
  // Side by side on desktop, one column on a phone; never past the viewport.
  const a = (await limit.boundingBox())!;
  const b = (await reserve.boundingBox())!;
  if (test.info().project.name === "desktop") expect(Math.abs(a.y - b.y)).toBeLessThan(2);
  else expect(b.y).toBeGreaterThan(a.y + a.height);
  const vw = await page.evaluate(() => document.documentElement.clientWidth);
  expect(b.x + b.width).toBeLessThanOrEqual(vw);
  await shot(page, "session-limit");

  // Lowering the limit under the reserve: the reason is said and nothing is sent.
  await reserve.fill("3");
  await limit.fill("2");
  await expect(card.getByTestId("session-reserve-refusal")).toHaveText(
    "The reserve can't be more than the session limit (2).",
  );
  const save = page.getByRole("button", { name: "Save limits" });
  await expect(save).toBeDisabled();
  await shot(page, "session-limit-refused");

  await limit.fill("8");
  await expect(card.getByTestId("session-reserve-help")).toHaveText(
    "Other starts can use 5 of 8; automations can use any free slot. At least 1.",
  );
  await save.click();
  await expect(page.getByRole("status").filter({ hasText: "Saved for new launches" })).toBeVisible();
  expect(writes).toEqual([{ max_sessions: 8, session_reserved_automations: 3 }]);
});

test("a session limit reserved whole says only automations can start sessions", async ({ page }) => {
  const { card } = await sessionLimit(page, 2, 2);
  await expect(card.getByTestId("session-reserve-help")).toHaveText(
    "Only automations can start sessions: all 2 are reserved for them. At least 1.",
  );
  await shot(page, "session-limit-automations-only");
});

// ---- Missions → Automations: the queue line and a queued run ------------------------------------

test("the list says how many steps are queued, and nothing when none are", async ({ page }) => {
  await mockAutomations(page, { automations: [automation()], queueLength: 3 });
  await page.goto(AUTOMATIONS_PATH);
  const queued = page.getByTestId("automations-queued");
  await expect(queued).toHaveText("3 queued · waiting for a free slot");
  // The idle grey, never a failure's red or a warning's amber.
  const led = queued.locator("span").first();
  const colours = await led.evaluate((el) => {
    const css = getComputedStyle(document.documentElement);
    const probe = document.createElement("i");
    document.body.append(probe);
    const resolve = (v: string) => {
      probe.style.color = css.getPropertyValue(v);
      return getComputedStyle(probe).color;
    };
    const out = { led: getComputedStyle(el).backgroundColor, idle: resolve("--status-unknown") };
    probe.remove();
    return out;
  });
  expect(colours.led).toBe(colours.idle);
  await shot(page, "list-queued");
});

test("an empty queue shows no queue line", async ({ page }) => {
  await mockAutomations(page, { automations: [automation()], queueLength: 0 });
  await page.goto(AUTOMATIONS_PATH);
  await expect(page.getByTestId("automations-summary")).toBeVisible();
  await expect(page.getByTestId("automations-queued")).toHaveCount(0);
  await expect(page.getByTestId("automations-page")).not.toContainText("queued");
});

test("a queued run says it waits for a free slot, where it stands, and that nothing failed", async ({ page }) => {
  const now = Math.floor(Date.now() / 1000);
  const since = now - 120;
  const queuedRun = {
    id: "rq",
    automation_id: "a1",
    trigger: "schedule",
    slot: "2026-11-27T21:14",
    fire_at: since,
    catch_up: false,
    covered: 1,
    state: "queued",
    outcome: "",
    result_class: "pending",
    reason: "",
    mission_id: null,
    session_key: null,
    created_at: since,
    finished_at: null,
    inputs: {},
    scope: {},
    queue: { position: 2, since, capacity: "session_limit", detail: "the session limit is full (6 of 6 running)" },
    steps: [
      { seq: 1, at: since, step: "claimed", detail: "slot 2026-11-27T21:14" },
      { seq: 2, at: since, step: "queued", detail: "session limit 6 of 6" },
    ],
  };
  const done = { ...queuedRun, id: "r1", state: "done", outcome: "ok", result_class: "ok", reason: "session started and briefed", created_at: now - 86400, finished_at: now - 86400, queue: null, steps: [] };
  await mockAutomations(page, { automations: [automation({ last_run: queuedRun })], runs: { a1: [queuedRun, done] } });
  await page.route("**/api/sessions/capacity", (r) =>
    r.fulfill({ json: { limit: 6, recommended: 6, source: "recommended", readable: true, running: 6, others: 0, sessions: [] } }),
  );
  await page.goto(automationPath("a1"));
  const hhmm = await page.evaluate((t) => new Date(t * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }), since);
  const row = page.getByTestId("run-row").first();
  await expect(row).toContainText(`Queued for a free slot · since ${hhmm} · 2nd in line`);
  await expect(row.getByTestId("run-outcome")).toHaveText("Queued");
  const detail = page.getByTestId("run-detail");
  await expect(detail.getByTestId("run-outcome")).toHaveText("Queued");
  await expect(detail).toContainText("Queued for a free slot · 2nd in line");
  const notice = detail.getByTestId("run-queued");
  await expect(notice).toContainText("Waiting for a free slot");
  await expect(notice).toContainText(
    "The session limit is full (6 of 6 running). This step starts on its own as soon as a slot frees; nothing has failed.",
  );
  await expect(detail.locator('[data-step="queued"]')).toContainText("Queued for a free slot · session limit 6 of 6");
  const see = notice.getByRole("button", { name: "See what’s running" });
  expect((await see.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await shot(page, "run-queued");
  await see.click();
  await expect(page.getByTestId("session-limit-dialog")).toBeVisible();

  // The list's last run says the same, in the same words.
  await page.goto(AUTOMATIONS_PATH);
  await expect(page.getByTestId("automations-page")).toContainText(`Queued for a free slot · since ${hhmm} · 2nd in line`);
});

// ---- captures, light theme (the functional tests above shoot the default dark) -------------------

test("captures the queue surfaces in the light theme", async ({ page }) => {
  test.setTimeout(60_000);
  await page.addInitScript(() => localStorage.setItem("tr-theme", "light"));
  await pool(page, { limit: 6, reserve: 1 });
  await shot(page, "pool-light");
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await sessionLimit(page, 6, 1);
  await shot(page, "session-limit-light");
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await mockAutomations(page, { automations: [automation()], queueLength: 3 });
  await page.goto(AUTOMATIONS_PATH);
  await expect(page.getByTestId("automations-queued")).toBeVisible();
  await shot(page, "list-queued-light");
});
