import { expect, test, type Locator } from "@playwright/test";
import { ACTIVE, ID, mockStructuredGuidance } from "./structuredGuidanceFixture";

// A process in an API session ran out of memory and was killed (#1732): the kernel killed one
// process, not the session, and the pane says so with #1651's memory banner treatment — on the
// turn that was running both times the worker read the counter, or between turns when it can't
// tell. A count, never a process name. Red on origin/main: the snapshot's `oom_kills` /
// `oom_kills_after` / `oom_kills_start` were never rendered.
const ENGINE = "codex-api";
const EARLIER = "33333333-2222-4333-8444-555555555555";

async function setup(page: Parameters<typeof mockStructuredGuidance>[0], theme: string) {
  const { state } = await mockStructuredGuidance(page, ENGINE, "steer", theme);
  const turns = state.turns as Record<string, unknown>[];
  turns.find((t) => t.turn_id === ACTIVE)!.oom_kills = 1;
  turns.unshift({
    turn_id: EARLIER, operation_id: EARLIER, state: "completed", text: "Run the browser suite.",
    reply: "The suite ran.", text_truncated: false, reply_truncated: false, tools_truncated: false,
    reason: null, tools: [], ts: Date.parse("2026-10-12T07:20:00Z") / 1000, oom_kills_after: 2,
  });
  Object.assign(state, { oom_kills_start: 1 });
  return state;
}

// WCAG contrast of an element's text against the first opaque background up its tree.
async function contrast(l: Locator): Promise<number> {
  return l.evaluate((el) => {
    const rgb = (c: string) => {
      const n = (c.match(/[\d.]+/g) ?? []).map(Number);
      return c.startsWith("color(srgb") ? n.map((v, i) => (i < 3 ? v * 255 : v)) : n;
    };
    const lum = ([r, g, b]: number[]) => {
      const f = (v: number) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    let bg: number[] = [];
    for (let n: Element | null = el; n; n = n.parentElement) {
      const c = rgb(getComputedStyle(n).backgroundColor);
      if (c.length === 3 || (c.length === 4 && c[3] === 1)) { bg = c.slice(0, 3); break; }
    }
    const fg = rgb(getComputedStyle(el).color).slice(0, 3);
    const [a, b] = [lum(fg), lum(bg.length ? bg : [255, 255, 255])].sort((x, y) => y - x);
    return (a + 0.05) / (b + 0.05);
  });
}

for (const theme of ["dark", "light"]) {
  test(`${theme}: an OOM kill is shown on its turn, between turns, and before the first`, async ({ page }, testInfo) => {
    await setup(page, theme);
    await page.goto(`/s/${ENGINE}/${ID}`);
    const turns = page.getByTestId("structured-turn");
    await expect(turns).toHaveCount(3);

    // On the running turn: killed while it ran. It names no process and changes no status.
    const active = turns.nth(1);
    const own = active.getByTestId("structured-oom");
    await expect(own).toHaveText("Out of memory A process in this session ran out of memory and was killed.");
    await expect(own).toHaveAttribute("data-count", "1");
    await expect(active.getByTestId("structured-working")).toBeVisible();

    // Between the earlier turn and the running one: a session-level notice, inside neither turn.
    const between = page.getByTestId("structured-oom-between");
    await expect(between).toHaveCount(2);
    const after = between.nth(1);
    await expect(after).toHaveText("Out of memory 2 processes in this session ran out of memory and were killed.");
    for (const turn of [turns.nth(0), active]) {
      await expect(turn.getByTestId("structured-oom-between")).toHaveCount(0);
    }
    const [earlierBox, afterBox, activeBox] = await Promise.all(
      [turns.nth(0), after, active].map(async (l) => (await l.boundingBox())!),
    );
    expect(afterBox.y).toBeGreaterThanOrEqual(earlierBox.y + earlierBox.height - 1);
    expect(afterBox.y + afterBox.height).toBeLessThanOrEqual(activeBox.y + 1);

    // Before the first turn: shown above it.
    const start = between.nth(0);
    await expect(start).toHaveText("Out of memory A process in this session ran out of memory and was killed.");
    expect((await start.boundingBox())!.y).toBeLessThan(earlierBox.y);

    for (const notice of [own, after, start]) {
      expect(await contrast(notice.locator("b"))).toBeGreaterThanOrEqual(4.5);
      expect(await contrast(notice.locator("p"))).toBeGreaterThanOrEqual(4.5);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
    await page.screenshot({ path: testInfo.outputPath(`oom-notice-${theme}.png`), fullPage: true });
  });
}
