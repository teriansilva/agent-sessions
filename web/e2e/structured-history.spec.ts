import { expect, test, type Page } from "@playwright/test";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

// #1504: an API session's pane shows the snapshot's newest 50 turns; scrolling up past them loads
// the turns before, a page at a time, to the first. In a real browser, desktop and mobile:
// nothing loads before a gesture, the turn being read stays where it was when a page lands, a
// failed page offers Retry by keyboard, a lost cursor keeps what was loaded, a refresh that
// slides the window keeps it too, and nothing scrolls sideways. The server is mocked.

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const URL_PATH = `/s/codex-api/${ID}`;
const SESSION = new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}(/[a-z]+)?(\\?.*)?$`);
const WINDOW = 50;

const tid = (i: number) => `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`;
const index = (id: string) => Number(id.slice(-12));

function turn(i: number) {
  return {
    kind: "operator",
    turn_id: tid(i),
    operation_id: tid(i),
    state: "completed",
    text: `Message ${i}`,
    ts: 1_790_000_000 + i * 60,
    reply: `Reply ${i}: done.`,
    reply_ts: 1_790_000_030 + i * 60,
    text_truncated: false,
    reply_truncated: false,
    reason: null,
    tools: [],
    tools_truncated: false,
    attachments: [],
    context: {},
  };
}

interface Server {
  total: number;
  revision: number;
  /** Cursors the server no longer knows (a 409). */
  gone: Set<string>;
  /** Status to answer the next history read with instead of a page. */
  failNext: number | null;
  /** While set, history reads wait for it. */
  hold: Promise<void> | null;
  reads: string[];
  /** An older turn the snapshot still shows ahead of its window, because it is live (queued). */
  pulled?: number;
}

function snapshot(s: Server) {
  const first = Math.max(0, s.total - WINDOW);
  return {
    session_key: `codex-api:${ID}`,
    revision: s.revision,
    event_cursor: s.revision,
    cwd: "/home/u/proj",
    state: "idle",
    active_turn: null,
    model_requested: null,
    model_effective: "gpt-5-codex",
    turns: [
      ...(s.pulled !== undefined && s.pulled < first
        ? [{ ...turn(s.pulled), state: "queued", reply: "", reply_ts: null }]
        : []),
      ...Array.from({ length: s.total - first }, (_, k) => turn(first + k)),
    ],
    omitted_turns: first,
    history_before: first > 0 ? tid(first) : null,
    pending_requests: [],
    native: { native_id: "n1", worker: "w1", background_active: false },
    read_only: null,
  };
}

async function setup(page: Page, theme: "dark" | "light", total = 120): Promise<Server> {
  const s: Server = { total, revision: 4, gone: new Set(), failNext: null, hold: null, reads: [] };
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["codex-api"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
        theme,
        agent_defaults: { bypass: false },
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await mockRoster(page, {
    overrides: {
      "codex-api": {
        present: true,
        supports_new: true,
        api: { kind: "codex-app-server", source: "codex", unavailable_reason: null, can_bypass: true },
      },
    },
  });
  await page.route(SESSION, async (r) => {
    const url = new URL(r.request().url());
    const sub = url.pathname.match(/\/(events|history)$/)?.[1] ?? "";
    if (sub === "events")
      return r.fulfill({
        json: { session_key: `codex-api:${ID}`, revision: s.revision, next_cursor: s.revision, events: [] },
      });
    if (sub === "history") {
      const before = url.searchParams.get("before") ?? "";
      const limit = Number(url.searchParams.get("limit") ?? 50);
      s.reads.push(before);
      if (s.hold) await s.hold;
      if (s.failNext !== null) {
        const status = s.failNext;
        s.failNext = null;
        return r.fulfill({ status, json: { detail: "unavailable" } });
      }
      if (s.gone.has(before)) return r.fulfill({ status: 409, json: { detail: "history cursor not found" } });
      const at = index(before);
      const start = Math.max(0, at - limit);
      const turns = Array.from({ length: at - start }, (_, k) => turn(start + k));
      return r.fulfill({
        json: {
          session_key: `codex-api:${ID}`,
          turns,
          before: start > 0 ? tid(start) : null,
          has_more: start > 0,
        },
      });
    }
    if (sub === "") return r.fulfill({ json: snapshot(s) });
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
  return s;
}

const log = (page: Page) => page.getByTestId("api-log");
const message = (page: Page, i: number) => log(page).getByText(`Message ${i}`, { exact: true });

/** Reach the top of the log the way the operator does.
 *
 *  Desktop: the wheel over the log. Mobile: headless Chromium does not scroll on synthetic touch
 *  gestures, so the log is put at its top with NO input behind it (which by itself must ask for
 *  nothing), and then a real touch swipe down — the pull an operator makes at the top. */
async function toTop(page: Page, isMobile: boolean) {
  const box = (await log(page).boundingBox())!;
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  if (isMobile) {
    await log(page).evaluate((el) => (el.scrollTop = 0));
    const cdp = await page.context().newCDPSession(page);
    const y0 = box.y + 40;
    await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y: y0 }] });
    for (let k = 1; k <= 6; k += 1)
      await cdp.send("Input.dispatchTouchEvent", {
        type: "touchMove",
        touchPoints: [{ x, y: y0 + k * 30 }],
      });
    await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await cdp.detach();
  } else {
    await page.mouse.move(x, y);
    await expect(async () => {
      await page.mouse.wheel(0, -4000);
      expect(await log(page).evaluate((el) => el.scrollTop)).toBeLessThanOrEqual(2);
    }).toPass({ timeout: 15_000 });
  }
  await settled(page);
}

/** Until the log stops moving: a scroll animation can outlast the gesture that started it. */
async function settled(page: Page) {
  let last = -1;
  await expect
    .poll(
      async () => {
        const now = await log(page).evaluate((el) => el.scrollTop);
        const still = now === last;
        last = now;
        return still;
      },
      { intervals: [150] },
    )
    .toBe(true);
}

/** Hold the next history reads until released, so a position can be read before a page lands. */
function hold(s: Server): () => void {
  let release!: () => void;
  s.hold = new Promise((r) => (release = r));
  return () => {
    s.hold = null;
    release();
  };
}

/** The first page, held and released: the log then holds turns 20…119. */
async function firstPage(page: Page, s: Server, isMobile: boolean) {
  const release = hold(s);
  await toTop(page, isMobile);
  await expect.poll(() => s.reads.length).toBe(1);
  release();
  await expect(message(page, 20)).toBeAttached();
  await settled(page);
}

async function noSidewaysScroll(page: Page) {
  const { scroll, inner } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    inner: window.innerWidth,
  }));
  expect(scroll).toBeLessThanOrEqual(inner);
}

for (const theme of ["dark", "light"] as const) {
  test(`${theme}: scrolling up loads earlier turns, keeps the reading position, and reaches the beginning`, async ({
    page,
    isMobile,
  }) => {
    const s = await setup(page, theme);
    await page.goto(URL_PATH);
    await expect(message(page, 119)).toBeVisible();
    // Opening the pane and following the bottom fetch nothing older; nor does a scroll to the
    // top that no operator input made.
    await page.waitForTimeout(1_000);
    await log(page).evaluate((el) => (el.scrollTop = 0));
    await page.waitForTimeout(1_000);
    expect(s.reads).toEqual([]);
    await expect(page.getByText(/earlier turns are not shown/)).toHaveCount(0);
    await log(page).evaluate((el) => (el.scrollTop = el.scrollHeight));
    await settled(page);

    // Hold the page so the reading position can be measured before and after it lands.
    const release = hold(s);
    await toTop(page, isMobile);
    await expect(page.getByTestId("api-history")).toHaveText("Loading earlier turns…");
    await expect.poll(() => s.reads.length).toBe(1);
    expect(s.reads[0]).toBe(tid(70));
    const anchor = message(page, 70);
    const before = (await anchor.boundingBox())!.y;
    release();
    await expect(message(page, 20)).toBeAttached();
    await expect(page.getByTestId("api-history-seam")).toHaveText("earlier turns ↑");
    // The turn being read is where it was, within a few px.
    await expect.poll(async () => Math.abs((await anchor.boundingBox())!.y - before)).toBeLessThanOrEqual(4);
    // One page per approach: the restored position asked for nothing more.
    await page.waitForTimeout(800);
    expect(s.reads).toHaveLength(1);

    await toTop(page, isMobile);
    await expect(message(page, 0)).toBeAttached();
    await expect(page.getByTestId("api-history")).toHaveText("Beginning of conversation");
    expect(s.reads).toEqual([tid(70), tid(20)]);
    // Every turn once.
    for (const i of [0, 19, 20, 69, 70, 119]) await expect(message(page, i)).toHaveCount(1);
    // History is not an arrival (#1727): no prepended page, nor the first paint, animated…
    await expect(log(page).locator(".nj-arrive")).toHaveCount(0);
    // …while a turn the conversation gains now does.
    s.total += 1;
    s.revision += 1;
    await expect(message(page, 120)).toBeAttached();
    await expect(log(page).locator(".nj-arrive")).toHaveCount(1);
    await expect(
      log(page).getByTestId("structured-turn").filter({ has: page.getByText("Message 120", { exact: true }) }),
    ).toHaveClass(/nj-arrive/);
    await noSidewaysScroll(page);
  });

  test(`${theme}: a failed page offers Retry — readable, 44px, by keyboard`, async ({ page, isMobile }) => {
    const s = await setup(page, theme);
    s.failNext = 503;
    await page.goto(URL_PATH);
    await expect(message(page, 119)).toBeVisible();
    await toTop(page, isMobile);
    const retry = page.getByTestId("api-history-retry");
    await expect(retry).toBeVisible();
    await expect(page.getByRole("alert")).toHaveText("Couldn't load earlier turns.");
    expect((await retry.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    // The label reads the accent's text token, at a contrast that passes for normal text.
    const look = await retry.evaluate((el) => {
      const probe = document.createElement("span");
      probe.style.color = "var(--accent-text)";
      el.appendChild(probe);
      const accentText = getComputedStyle(probe).color;
      probe.remove();
      const cs = getComputedStyle(el);
      const rgb = (c: string) => c.match(/[\d.]+/g)!.slice(0, 3).map(Number);
      const lum = (c: string) => {
        const [r, g, b] = rgb(c).map((v) => {
          const x = v / 255;
          return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4;
        });
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
      };
      const [a, b] = [lum(cs.color), lum(cs.backgroundColor)].sort((x, y) => y - x);
      return { color: cs.color, accentText, ratio: (a + 0.05) / (b + 0.05) };
    });
    expect(look.color).toBe(look.accentText);
    expect(look.ratio).toBeGreaterThanOrEqual(4.5);
    // Keyboard: Tab reaches it with a visible ring, Enter loads the page.
    await retry.focus();
    await page.keyboard.press("Shift+Tab");
    await page.keyboard.press("Tab");
    await expect(retry).toBeFocused();
    expect(await retry.evaluate((el) => getComputedStyle(el).outlineStyle)).not.toBe("none");
    await page.keyboard.press("Enter");
    await expect(message(page, 20)).toBeAttached();
    await expect(retry).toHaveCount(0);
    expect(s.reads).toEqual([tid(70), tid(70)]);
    await noSidewaysScroll(page);
  });
}

test("a lost cursor keeps what was loaded and the reading position", async ({ page, isMobile }) => {
  const s = await setup(page, "dark");
  await page.goto(URL_PATH);
  await expect(message(page, 119)).toBeVisible();
  await firstPage(page, s, isMobile);
  // The oldest loaded turn, the next cursor, is gone from the server's history.
  s.gone.add(tid(20));
  const release = hold(s);
  await toTop(page, isMobile);
  await expect.poll(() => s.reads.length).toBe(2);
  const anchor = message(page, 20);
  const before = (await anchor.boundingBox())!.y;
  release();
  await expect(message(page, 0)).toBeAttached();
  // It retried from the next turn it holds; nothing loaded was dropped or doubled.
  expect(s.reads).toEqual([tid(70), tid(20), tid(21)]);
  for (const i of [0, 20, 21, 69, 70]) await expect(message(page, i)).toHaveCount(1);
  await expect.poll(async () => Math.abs((await anchor.boundingBox())!.y - before)).toBeLessThanOrEqual(4);
  await noSidewaysScroll(page);
});

test("a refresh that slides the window keeps the loaded turns, each once", async ({ page, isMobile }) => {
  const s = await setup(page, "light");
  await page.goto(URL_PATH);
  await expect(message(page, 119)).toBeVisible();
  await firstPage(page, s, isMobile);
  // Two new turns: the window now starts at 72, and 70 and 71 left it.
  s.total = 122;
  s.revision += 1;
  await expect(message(page, 121)).toBeAttached({ timeout: 25_000 });
  await expect.poll(() => s.reads.at(-1), { timeout: 15_000 }).toBe(tid(72));
  expect(s.reads).toEqual([tid(70), tid(72)]);
  for (const i of [20, 69, 70, 71, 72, 121]) await expect(message(page, i)).toHaveCount(1);
  await noSidewaysScroll(page);
});

test("a live turn the snapshot shows ahead of its window stays above newer history", async ({
  page,
  isMobile,
}) => {
  // Hermes on #1516: queued turn 10 is shown ahead of the window 70–119; page 20–69 must load
  // BELOW it, never above it. Once a page covers it, it sits between 9 and 11, once.
  const s = await setup(page, "dark");
  s.pulled = 10;
  await page.goto(URL_PATH);
  await expect(message(page, 119)).toBeVisible();
  await firstPage(page, s, isMobile);
  const y = async (i: number) => (await message(page, i).boundingBox())!.y;
  await expect(message(page, 10)).toHaveCount(1);
  expect(await y(10)).toBeLessThan(await y(20));
  expect(await y(69)).toBeLessThan(await y(70));
  await toTop(page, isMobile);
  await expect(message(page, 0)).toBeAttached();
  await expect(message(page, 10)).toHaveCount(1);
  expect(await y(9)).toBeLessThan(await y(10));
  expect(await y(10)).toBeLessThan(await y(11));
  await noSidewaysScroll(page);
});
