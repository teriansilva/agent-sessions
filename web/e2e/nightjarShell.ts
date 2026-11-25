/** A mocked shell for the NIGHTJAR specs (#1531): enough of the API for the dashboard, the
 *  primitives gallery and Settings to render with no backend. Every unknown `/api/**` answers `{}`. */
import { expect, type Page, type TestInfo } from "@playwright/test";

export async function mockNightjarShell(
  page: Page,
  opts: { hqContact?: string } = {},
) {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        username: null,
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
        hq_contact: opts.hqContact ?? "romania-woman",
        hq_contact_rev: 1,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        total: 0,
        next_offset: null,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route(/\/api\/projects($|\?)/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({
      json: { notifications: [], unread: 0, uncertain: 0, settled: [] },
    }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({ json: { missions: [], total: 0, next_offset: null } }),
  );
}

/** Store a motion choice for the page's origin BEFORE the app boots (the pre-paint script reads it). */
export async function storeMotionChoice(
  page: Page,
  choice: "full" | "reduced" | "off" | null,
) {
  await page.addInitScript((c) => {
    try {
      if (c) localStorage.setItem("tr-motion", c);
      else localStorage.removeItem("tr-motion");
    } catch {
      /* storage blocked */
    }
  }, choice);
}

/** Wait until every FINITE animation on the page has finished (#1531). Sheets slide up and dialogs
 *  deploy in over 120–320 ms; a geometry assertion or a corner tap taken during that window
 *  measures a moving box. Infinite ones (a pulse, a spinner) are ignored: they never finish. */
export async function animationsSettled(page: Page) {
  await page.waitForFunction(() =>
    document
      .getAnimations()
      .filter((a) => a.effect?.getComputedTiming().iterations !== Infinity)
      .every((a) => a.playState === "finished" || a.playState === "idle"),
  );
}

/** What an idle surface costs the main thread (#1531, #1573), from CDP Performance metrics over
 *  `windows` back-to-back windows of `ms`:
 *  - `busy`: TaskDuration over wall time, the QUIETEST window (other jobs on a shared machine can
 *    only stretch a window, never shrink it);
 *  - `cpu`: the main thread's own CPU time (ThreadTime) over wall time, the QUIETEST window. A task
 *    descheduled behind other jobs is stretched in TaskDuration but not in ThreadTime, so this is
 *    the duration bound a shared runner can judge (a 40 ms script once a second reads ~4%);
 *  - `recalcPerS` / `layoutPerS`: style recalcs and layouts per second, the BUSIEST window. These
 *    are counts, not durations: a slow or loaded machine does not raise them, it can only drop
 *    frames. A main-thread animation recalculates style every frame (~60/s) and a script loop lays
 *    out every frame; a surface whose motion is the compositor's stays near the steps() it uses. */
export async function idleProfile(
  page: Page,
  label: string,
  { windows = 3, ms = 3000 }: { windows?: number; ms?: number } = {},
): Promise<{ busy: number; cpu: number; recalcPerS: number; layoutPerS: number }> {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Performance.enable");
  const read = async () => {
    const { metrics } = await cdp.send("Performance.getMetrics");
    return Object.fromEntries(metrics.map((m) => [m.name, m.value])) as Record<string, number>;
  };
  const busy: number[] = [];
  const cpu: number[] = [];
  const recalc: number[] = [];
  const layout: number[] = [];
  for (let i = 0; i < windows; i++) {
    const a = await read();
    await page.waitForTimeout(ms);
    const b = await read();
    const dt = b.Timestamp - a.Timestamp;
    busy.push((b.TaskDuration - a.TaskDuration) / dt);
    cpu.push((b.ThreadTime - a.ThreadTime) / dt);
    recalc.push((b.RecalcStyleCount - a.RecalcStyleCount) / dt);
    layout.push((b.LayoutCount - a.LayoutCount) / dt);
  }
  const out = {
    busy: Math.min(...busy),
    cpu: Math.min(...cpu),
    recalcPerS: Math.max(...recalc),
    layoutPerS: Math.max(...layout),
  };
  console.log(
    `### ${label} idle main-thread busy in Full: ${(out.busy * 100).toFixed(2)}% ` +
      `(windows: ${busy.map((x) => (x * 100).toFixed(2)).join(", ")}%); ` +
      `main-thread CPU ${(out.cpu * 100).toFixed(2)}% (windows: ${cpu.map((x) => (x * 100).toFixed(2)).join(", ")}%); ` +
      `style recalcs ${out.recalcPerS.toFixed(1)}/s, layouts ${out.layoutPerS.toFixed(1)}/s`,
  );
  return out;
}

/** The idle budget (docs/design.md v2 §8), one contract on every machine, CI included:
 *  - main-thread CPU (ThreadTime) under 2% — the duration bound a shared runner can judge, since
 *    descheduling does not inflate it (TaskDuration read 2.06% and then 4.03% there for one commit);
 *  - at most 20 style recalcs and 2 layouts per second — what a main-thread animation (~60/s) or a
 *    layout loop breaks, whatever the machine;
 *  - on an idle host (not CI) also main-thread busy (TaskDuration) under 2%. */
export const IDLE_RECALC_MAX = 20;
export const IDLE_LAYOUT_MAX = 2;
export async function expectIdleBudget(page: Page, label: string, info: TestInfo): Promise<void> {
  const p = await idleProfile(page, label);
  info.annotations.push({
    type: "idle-busy",
    description: `${(p.busy * 100).toFixed(2)}% busy · ${(p.cpu * 100).toFixed(2)}% CPU · ${p.recalcPerS.toFixed(1)} recalcs/s · ${p.layoutPerS.toFixed(1)} layouts/s`,
  });
  expect(p.cpu, `${label}: main-thread CPU while idle`).toBeLessThan(0.02);
  expect(p.recalcPerS, `${label}: style recalcs per second while idle`).toBeLessThanOrEqual(IDLE_RECALC_MAX);
  expect(p.layoutPerS, `${label}: layouts per second while idle`).toBeLessThanOrEqual(IDLE_LAYOUT_MAX);
  if (!process.env.CI) expect(p.busy, `${label}: main-thread busy while idle`).toBeLessThan(0.02);
}
