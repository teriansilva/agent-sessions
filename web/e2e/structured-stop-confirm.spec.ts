import { expect, test, type Locator, type Page } from "@playwright/test";

import { ID, mockStructuredGuidance } from "./structuredGuidanceFixture";
import { TOUCH_FLOOR } from "./touchTarget";

// #1724: an API session stays alive while background work runs, so Stop asks first and ends only
// what the operator confirmed. The server rechecks the list; a changed one is refused (409) with
// the new list, and nothing is stopped before a confirmation of what runs now.
const NOW = Math.floor(Date.now() / 1000);
const D1 = "1".repeat(64);
const D2 = "2".repeat(64);
const D_UNKNOWN = "3".repeat(64);
const D_EMPTY = "4".repeat(64);
const TASK = { task_id: "bq7", description: "pytest -x", task_type: "local_bash", started_at: NOW - 240 };
const NODE = { pid: 4121, start: 9001, comm: "node", started_at: NOW - 960 };
const CARGO = { pid: 4188, start: 9500, comm: "cargo", started_at: NOW - 20 };

function inventory(over: Record<string, unknown> = {}) {
  return { worker: "worker", digest: D1, scan: "ok", tasks: [TASK], processes: [NODE], more: 0, observed_at: NOW, ...over };
}

async function stopSession(page: Page, theme: string, stopInventory: Record<string, unknown>, answers: ((body: unknown) => { status: number; json: unknown })[]) {
  const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt", theme);
  fixture.state.state = "idle";
  fixture.state.active_turn = "";
  fixture.state.turns = [{ ...fixture.state.turns[0], state: "completed" }];
  const processes = (stopInventory.processes as typeof NODE[]) ?? [];
  Object.assign(fixture.state.native, {
    background_active: true,
    background_tasks: [
      ...(stopInventory.tasks as typeof TASK[]).map((t) => ({ ...t, source: "engine", state: "running", updated_at: NOW })),
      ...processes.map((p) => ({ task_id: `pid:${p.pid}:${p.start}`, source: "process", state: "running", pid: p.pid, comm: p.comm, started_at: p.started_at, updated_at: NOW })),
    ],
    stop_inventory: stopInventory,
    worker_ends: [],
  });
  const stops: unknown[] = [];
  await page.route(new RegExp(`/api/structured/sessions/claude-api(?::|%3A)${ID}/stop$`), async (r) => {
    const body = r.request().postDataJSON();
    stops.push(body);
    const answer = answers[Math.min(stops.length - 1, answers.length - 1)](body);
    if (answer.status === 200) fixture.state.native.worker = null as unknown as string;
    fixture.state.revision++;
    return r.fulfill({ status: answer.status, json: answer.json });
  });
  await page.goto(`/s/claude-api/${ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  return { fixture, stops };
}

async function pressStop(page: Page) {
  const pane = page.getByTestId("structured-pane");
  if ((page.viewportSize()?.width ?? 1200) <= 800) {
    await pane.getByTestId("head-actions-menu").click();
    await page.getByRole("menuitem", { name: "Stop" }).click();
  } else {
    await pane.getByRole("button", { name: "Stop the worker" }).click();
  }
}

/** Keep running before Stop, read from ONE frame: the phone sheet slides up, and two separate
 *  `boundingBox()` reads can land on different animation frames. */
async function keepFirst(dialog: Locator, mobile: boolean): Promise<boolean> {
  return dialog.evaluate((el, phone) => {
    const buttons = [...el.querySelectorAll("button")];
    const keep = buttons.find((b) => b.textContent?.trim() === "Keep running")!.getBoundingClientRect();
    const go = el.querySelector('[data-testid="structured-stop-confirm-go"]')!.getBoundingClientRect();
    return phone ? keep.bottom <= go.top : keep.right <= go.left;
  }, mobile);
}

/** WCAG contrast of an element's text against the first opaque background behind it. */
async function contrast(locator: Locator): Promise<number> {
  return locator.evaluate((el) => {
    const rgb = (c: string) => (c.match(/[\d.]+/g) ?? []).map(Number);
    const lum = ([r, g, b]: number[]) => {
      const f = (v: number) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    const fg = rgb(getComputedStyle(el).color);
    let node: Element | null = el;
    let bg = [255, 255, 255];
    while (node) {
      const c = rgb(getComputedStyle(node).backgroundColor);
      if (c.length >= 3 && (c.length < 4 || c[3] >= 0.99)) { bg = c; break; }
      node = node.parentElement;
    }
    const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    return (a + 0.05) / (b + 0.05);
  });
}

for (const theme of ["dark", "light"]) {
  test(`#1724: Stop lists the background work, refreshes a changed list, and stops only on a valid confirmation (${theme})`, async ({ page }, testInfo) => {
    const refreshed = inventory({ digest: D2, processes: [NODE, CARGO] });
    const { stops } = await stopSession(page, theme, inventory(), [
      () => ({ status: 409, json: { detail: "changed", reason: "inventory_changed", inventory: refreshed } }),
      (body) => (body as { digest: string }).digest === D2
        ? { status: 200, json: { session_key: `claude-api:${ID}`, containment: "gone" } }
        : { status: 409, json: { detail: "changed", reason: "inventory_changed", inventory: refreshed } },
    ]);
    await pressStop(page);
    const dialog = page.getByTestId("structured-stop-confirm");
    await expect(dialog).toBeVisible();
    expect(stops).toHaveLength(0); // asking first: nothing was sent, nothing stopped
    await expect(dialog.getByText(/Stopping ends 2 background jobs/)).toBeVisible();
    await expect(dialog.getByTestId("structured-stop-tasks")).toContainText("pytest -x");
    await expect(dialog.getByTestId("structured-stop-processes")).toContainText("node · pid 4121");
    await expect(dialog.getByText(/npm|--port|argv/)).toHaveCount(0); // program and pid only
    const keep = dialog.getByRole("button", { name: "Keep running" });
    const go = dialog.getByTestId("structured-stop-confirm-go");
    await expect(keep).toBeFocused();
    await expect(go).toHaveText("Stop and end jobs");
    // Honest about the window: the stop also ends anything these jobs start meanwhile.
    await expect(dialog.getByTestId("structured-stop-also-ends")).toHaveText(
      "Anything these jobs start while Stop runs also ends.",
    );
    const [k, g] = [(await keep.boundingBox())!, (await go.boundingBox())!];
    expect(k.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    expect(g.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    // Keep running comes first on every viewport, the phone's bottom sheet included.
    expect(await keepFirst(dialog, testInfo.project.name === "mobile")).toBe(true);
    expect(await contrast(dialog.getByText("node · pid 4121"))).toBeGreaterThanOrEqual(4.5);
    expect(await contrast(dialog.getByText(/Stopping ends/))).toBeGreaterThanOrEqual(4.5);
    await page.screenshot({ path: testInfo.outputPath(`stop-confirm-${theme}.png`) });

    await go.click();
    // The server saw a new process: the dialog shows the current list and asks again.
    await expect(dialog.getByTestId("structured-stop-changed")).toBeVisible();
    await expect(dialog.getByText(/Stopping ends 3 background jobs/)).toBeVisible();
    const rows = dialog.getByTestId("structured-stop-processes").getByRole("listitem");
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(1)).toHaveAttribute("data-new", "");
    await expect(rows.nth(1)).toContainText("cargo · pid 4188");
    expect(stops).toEqual([{ worker: "worker", digest: D1 }]);
    await expect(page.getByTestId("structured-error")).toHaveCount(0);
    // The refresh also offers Stop everything, disclosed, so a looping job is one more tap.
    const everything = dialog.getByTestId("structured-stop-everything");
    await expect(everything).toHaveText("Stop everything");
    await expect(dialog.getByTestId("structured-stop-everything-disclosure")).toContainText(
      "including work started after this list or not listed here",
    );
    expect((await everything.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    await expect(keep).toBeFocused();
    expect(await keepFirst(dialog, testInfo.project.name === "mobile")).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`stop-confirm-refreshed-${theme}.png`) });
    await go.click();
    await expect(dialog).toHaveCount(0);
    expect(stops).toEqual([{ worker: "worker", digest: D1 }, { worker: "worker", digest: D2 }]);
  });
}

test("#1724: Keep running stops nothing", async ({ page }) => {
  const { stops } = await stopSession(page, "dark", inventory(), [() => ({ status: 500, json: {} })]);
  await pressStop(page);
  await page.getByTestId("structured-stop-confirm").getByRole("button", { name: "Keep running" }).click();
  await expect(page.getByTestId("structured-stop-confirm")).toHaveCount(0);
  expect(stops).toHaveLength(0);
});

test("#1724: a refresh's Stop everything ends a set that keeps changing in one more tap", async ({ page }) => {
  const looping = inventory({ digest: D2, processes: [NODE, CARGO] });
  const { stops } = await stopSession(page, "dark", inventory(), [
    () => ({ status: 409, json: { detail: "changed", reason: "inventory_changed", inventory: looping } }),
    (body) => (body as { everything?: boolean }).everything === true
      ? { status: 200, json: { session_key: `claude-api:${ID}`, containment: "gone" } }
      : { status: 409, json: { detail: "changed", reason: "inventory_changed", inventory: looping } },
  ]);
  await pressStop(page);
  const dialog = page.getByTestId("structured-stop-confirm");
  await dialog.getByTestId("structured-stop-confirm-go").click();
  await dialog.getByTestId("structured-stop-everything").click();
  await expect(dialog).toHaveCount(0);
  expect(stops).toEqual([{ worker: "worker", digest: D1 }, { worker: "worker", everything: true }]);
});

test("#1724: a list over the display limits shows the counts and offers only Stop everything", async ({ page }, testInfo) => {
  const { stops } = await stopSession(page, "dark", inventory({ more: 12, engine_more: 3 }), [
    () => ({ status: 200, json: { session_key: `claude-api:${ID}`, containment: "gone" } }),
  ]);
  await pressStop(page);
  const dialog = page.getByTestId("structured-stop-confirm");
  await expect(dialog).toHaveAttribute("data-kind", "everything");
  await expect(dialog.getByTestId("structured-stop-more-processes")).toHaveText("and 12 more processes");
  await expect(dialog.getByTestId("structured-stop-more-tasks")).toHaveText("and 3 more tasks");
  await expect(dialog.getByTestId("structured-stop-everything-disclosure")).toContainText("not listed here");
  await expect(dialog.getByTestId("structured-stop-everything")).toHaveCount(0);
  const keep = dialog.getByRole("button", { name: "Keep running" });
  const go = dialog.getByTestId("structured-stop-confirm-go");
  await expect(keep).toBeFocused();
  await expect(go).toHaveText("Stop everything");
  expect((await go.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect((await keep.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(await keepFirst(dialog, testInfo.project.name === "mobile")).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("stop-confirm-over-limit.png") });
  await go.click();
  await expect(dialog).toHaveCount(0);
  expect(stops).toEqual([{ worker: "worker", everything: true }]);
});

test("#1724: an unreadable process scan is disclosed and confirmed only by Stop everything", async ({ page }, testInfo) => {
  const { stops } = await stopSession(page, "light", inventory({ scan: "unknown", processes: [], digest: D_UNKNOWN }), [
    () => ({ status: 200, json: { session_key: `claude-api:${ID}`, containment: "gone" } }),
  ]);
  await pressStop(page);
  const dialog = page.getByTestId("structured-stop-confirm");
  await expect(dialog).toHaveAttribute("data-scan", "unknown");
  await expect(dialog.getByTestId("structured-stop-unknown")).toContainText("can’t list this session’s processes right now");
  await expect(dialog.getByTestId("structured-stop-everything-disclosure")).toContainText(
    "ends everything the agent started in this session — including work started after this list or not listed here",
  );
  await expect(dialog.getByTestId("structured-stop-tasks")).toContainText("pytest -x");
  await expect(dialog.getByText("Can’t be listed")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Keep running" })).toBeFocused();
  const go = dialog.getByTestId("structured-stop-confirm-go");
  await expect(go).toHaveText("Stop everything");
  await page.screenshot({ path: testInfo.outputPath("stop-confirm-unknown.png") });
  await go.click();
  await expect(dialog).toHaveCount(0);
  expect(stops).toEqual([{ worker: "worker", everything: true }]);
});

test("#1724: a worker that can't answer the check refuses with nothing stopped, then offers Stop everything for that generation", async ({ page }, testInfo) => {
  const hatch = { ...inventory(), reach: "unreachable" };
  const { stops } = await stopSession(page, "dark", inventory(), [
    () => ({ status: 503, json: { detail: "can't reach", reason: "worker_unreachable", inventory: hatch } }),
    (body) => (body as { everything?: boolean }).everything === true
      ? { status: 200, json: { session_key: `claude-api:${ID}`, containment: "gone" } }
      : { status: 500, json: { detail: "unexpected" } },
  ]);
  await pressStop(page);
  const dialog = page.getByTestId("structured-stop-confirm");
  await dialog.getByTestId("structured-stop-confirm-go").click();
  // The 503 is a refusal: the dialog stays, now as the escape hatch, and no error claims a stop.
  await expect(dialog).toHaveAttribute("data-reach", "unreachable");
  await expect(dialog.getByTestId("structured-stop-unreachable")).toContainText("can’t reach this session’s worker");
  await expect(dialog.getByTestId("structured-stop-everything-disclosure")).toContainText(
    "ends everything the agent started in this session — including work started after this list or not listed here",
  );
  await expect(dialog.getByTestId("structured-stop-tasks")).toContainText("pytest -x");
  await expect(page.getByTestId("structured-error")).toHaveCount(0);
  const keep = dialog.getByRole("button", { name: "Keep running" });
  const go = dialog.getByTestId("structured-stop-confirm-go");
  await expect(go).toHaveText("Stop everything");
  const [k, g] = [(await keep.boundingBox())!, (await go.boundingBox())!];
  expect(k.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(g.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(await keepFirst(dialog, testInfo.project.name === "mobile")).toBe(true);
  expect(await contrast(dialog.getByTestId("structured-stop-unreachable"))).toBeGreaterThanOrEqual(4.5);
  await page.screenshot({ path: testInfo.outputPath("stop-confirm-unreachable.png") });
  expect(stops).toEqual([{ worker: "worker", digest: D1 }]);
  await go.click();
  await expect(dialog).toHaveCount(0);
  expect(stops).toEqual([{ worker: "worker", digest: D1 }, { worker: "worker", everything: true }]);
});

test("#1724: an unreadable process scan is a visible row — it won't idle out — and its Stop offers Stop everything", async ({ page }, testInfo) => {
  const unknown = inventory({ scan: "unknown", processes: [], tasks: [], digest: D_UNKNOWN });
  const { stops } = await stopSession(page, "dark", unknown, [
    () => ({ status: 200, json: { session_key: `claude-api:${ID}`, containment: "gone" } }),
  ]);
  const row = page.getByTestId("structured-scan-unknown");
  await expect(row).toContainText("can’t list this session’s processes — it won’t idle out");
  const stop = row.getByRole("button", { name: "Stop…" });
  expect((await stop.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(await contrast(row.locator("span").nth(1))).toBeGreaterThanOrEqual(4.5);
  await page.screenshot({ path: testInfo.outputPath("scan-unknown-row.png") });
  await stop.click();
  const dialog = page.getByTestId("structured-stop-confirm");
  await expect(dialog).toHaveAttribute("data-scan", "unknown");
  await expect(dialog.getByRole("button", { name: "Keep running" })).toBeFocused();
  await dialog.getByTestId("structured-stop-confirm-go").click();
  await expect(dialog).toHaveCount(0);
  expect(stops).toEqual([{ worker: "worker", everything: true }]);
});

test("#1724: jobs an interrupted Stop could not resume are a row that leads to Stop everything", async ({ page }, testInfo) => {
  const { fixture, stops } = await stopSession(page, "dark", inventory(), [
    () => ({ status: 200, json: { session_key: `claude-api:${ID}`, containment: "gone" } }),
  ]);
  Object.assign(fixture.state.native, { paused_unresumed: 2 });
  fixture.state.revision++;
  const row = page.getByTestId("structured-paused-unresumed");
  await expect(row).toContainText("2 jobs were paused by an interrupted Stop and could not be resumed — Stop everything ends them");
  const button = row.getByRole("button", { name: "Stop everything…" });
  expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await page.screenshot({ path: testInfo.outputPath("paused-unresumed-row.png") });
  await button.click();
  const dialog = page.getByTestId("structured-stop-confirm");
  await expect(dialog).toHaveAttribute("data-kind", "everything");
  await expect(dialog.getByRole("button", { name: "Keep running" })).toBeFocused();
  await expect(dialog.getByTestId("structured-stop-everything-disclosure")).toContainText("not listed here");
  await dialog.getByTestId("structured-stop-confirm-go").click();
  await expect(dialog).toHaveCount(0);
  expect(stops).toEqual([{ worker: "worker", everything: true }]);
});

test("#1724: with no background work Stop acts at once, confirming the empty set", async ({ page }) => {
  const { stops } = await stopSession(page, "dark", inventory({ tasks: [], processes: [], digest: D_EMPTY }), [
    () => ({ status: 200, json: { session_key: `claude-api:${ID}`, containment: "gone" } }),
  ]);
  await pressStop(page);
  await expect.poll(() => stops.length).toBe(1);
  await expect(page.getByTestId("structured-stop-confirm")).toHaveCount(0);
  expect(stops).toEqual([{ worker: "worker", digest: D_EMPTY }]);
});

for (const theme of ["light", "dark"]) {
  test(`#1724: work that ended with its worker and an idle exit are transcript rows (${theme})`, async ({ page }, testInfo) => {
    const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt", theme);
    fixture.state.state = "idle";
    fixture.state.active_turn = "";
    const first = { ...fixture.state.turns[0], state: "completed" };
    fixture.state.turns = [first];
    Object.assign(fixture.state.native, {
      worker: null,
      worker_ends: [
        { worker: "w0", reason: "stop", at: NOW - 600, settled: true, after_turn: first.turn_id, scan: "ok",
          tasks: [{ task_id: "bq7", description: "pytest -x", source: "engine", state: "ended_with_worker" }],
          processes: [{ ...NODE, source: "process", state: "ended_with_worker" },
            { ...CARGO, started_during_stop: true, source: "process", state: "ended_with_worker" }], more: 0 },
        { worker: "w1", reason: "idle", at: NOW - 60, settled: true, after_turn: first.turn_id, idle_for: 1800 },
      ],
    });
    await page.goto(`/s/claude-api/${ID}`);
    const rows = page.getByTestId("structured-worker-end");
    await expect(rows).toHaveCount(4);
    await expect(rows.nth(0)).toContainText("Ended with worker");
    await expect(rows.nth(0)).toContainText("pytest -x · operator stop");
    await expect(rows.nth(1)).toContainText("node · pid 4121 · operator stop");
    await expect(rows.nth(2)).toContainText("cargo · pid 4188 · started while stopping · operator stop");
    await expect(rows.nth(3)).toContainText("Stopped after 30 min idle — no background work");
    // After the turn they follow, never inside it.
    const turn = (await page.getByTestId("structured-turn").first().boundingBox())!;
    expect((await rows.nth(0).boundingBox())!.y).toBeGreaterThanOrEqual(turn.y + turn.height - 1);
    for (const i of [0, 2, 3]) {
      expect(await contrast(rows.nth(i).locator("span").nth(2))).toBeGreaterThanOrEqual(4.5);
    }
    await page.screenshot({ path: testInfo.outputPath(`worker-ends-${theme}.png`) });
  });
}

test("#1724: end rows follow #1727's arrival rule — never on first paint, only when a generation ends while open", async ({ page }) => {
  const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt", "dark");
  fixture.state.state = "idle";
  fixture.state.active_turn = "";
  const first = { ...fixture.state.turns[0], state: "completed" };
  fixture.state.turns = [first];
  const earlier = { worker: "w0", reason: "idle", at: NOW - 600, settled: true, after_turn: first.turn_id, idle_for: 1800 };
  Object.assign(fixture.state.native, { worker: null, worker_ends: [earlier] });
  await page.goto(`/s/claude-api/${ID}`);
  const rows = page.getByTestId("structured-worker-end");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).not.toHaveClass(/nj-arrive/); // already there at first paint
  // A generation ends while the pane is open: its rows arrive.
  Object.assign(fixture.state.native, {
    worker_ends: [earlier, { worker: "w1", reason: "stop", at: NOW, settled: true, after_turn: first.turn_id,
      scan: "ok", tasks: [], processes: [{ ...NODE, source: "process", state: "ended_with_worker" }], more: 0 }],
  });
  fixture.state.revision++;
  await expect(rows).toHaveCount(2, { timeout: 20_000 });
  await expect(rows.nth(1)).toHaveClass(/nj-arrive/);
  await expect(rows.first()).not.toHaveClass(/nj-arrive/);
});

test("#1724: cut-off work after a steered turn shows after that group (Hermes)", async ({ page }) => {
  const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt", "light");
  fixture.state.state = "idle";
  fixture.state.active_turn = "";
  const main = { ...fixture.state.turns[0], state: "completed" };
  const steer = { ...fixture.state.turns[1], state: "delivered", delivery: "steered",
    delivery_turn_id: main.turn_id, text: "Also run the tests." };
  fixture.state.turns = [main, steer];
  Object.assign(fixture.state.native, {
    worker: null,
    worker_ends: [{ worker: "w0", reason: "stop", at: NOW, settled: true, after_turn: steer.turn_id,
      scan: "ok", tasks: [], processes: [{ ...NODE, source: "process", state: "ended_with_worker" }], more: 0 }],
  });
  await page.goto(`/s/claude-api/${ID}`);
  const row = page.getByTestId("structured-worker-end");
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("node · pid 4121 · operator stop");
  // After the group: below the turn (and its nested steer), never inside it.
  const group = (await page.getByTestId("structured-turn").first().boundingBox())!;
  expect((await row.boundingBox())!.y).toBeGreaterThanOrEqual(group.y + group.height - 1);
});
