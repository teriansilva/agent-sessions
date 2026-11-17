/** HQ shows it on screen (#2102), in a real browser, desktop and phone.
 *
 *  What a DOM emulator cannot prove: that a directive from HQ's stream really changes the route
 *  on screen, that a map window really opens (and only where the map can host one), that an
 *  unsaved-changes prompt really holds the navigation, that a reload or a second reader runs
 *  nothing, that the directives queued behind a navigation survive the view that asked for them
 *  being unmounted, and where keyboard focus ends up. Every test asserts the RENDERED outcome:
 *  the route or the window, and the words of the line under the answer.
 *
 *  The API is mocked. HQ's stream is a real `ReadableStream` the test feeds line by line (a
 *  `page.route` body lands all at once, which could not show a directive arriving after the
 *  page it came from has gone).
 */
import { expect, test, type Page } from "@playwright/test";

import { matchRoute } from "../src/components/ask/askMatches";
import { missionLink } from "../src/lib/missionLink";
import {
  AUTOMATIONS_PATH,
  DASHBOARD_PATH,
  HQ_PATH,
  MAP_PATH,
  TEMPLATES_PATH,
  artifactPath,
} from "../src/lib/routes";
import { settingsPath } from "../src/routes/settingsTabs";
import { openAsk, pickConversation } from "./askSidebar";
import { mockAskConversations, type FakeAsk, type FakeTurn } from "./askStream";
import { FAKE_MIC, connect, mockShell, say } from "./callFixture";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

// Chromium's fake microphone, for the call tests (a launch option, so file-wide).
test.use(FAKE_MIC);

const now = Math.floor(Date.now() / 1000);
const KEYS = [1, 2, 3, 4, 5].map((n) => `claude:s${n}`);
const sessions = KEYS.map((id, i) => ({
  id,
  engine: "claude",
  uuid: id.slice("claude:".length),
  short_uuid: id.slice("claude:".length),
  cwd: "/home/u/proj",
  project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
  last_mtime: now - (i + 1) * 60,
  first_user_message: "",
  title: `Window session ${i + 1}`,
  sticky: false,
  archived: false,
  ai_summary: "",
}));
const titleOf = (key: string) => sessions.find((s) => s.id === key)!.title;
const M1 = `msn_${"a".repeat(32)}`;
const A1 = "1234-5678-1234-5678";
const TEMPLATE_LIMITS = {
  templates_max: 200,
  name_max: 120,
  description_max: 300,
  tags_max: 8,
  body_max: 100_000,
  fields_max: 12,
  label_max: 60,
  default_max: 500,
  images_max: 8,
  image_suffixes: [".png"],
};

let seq = 0;
const directiveId = () => `sd_${(++seq).toString(16).padStart(16, "0")}`;
type Directive = Record<string, unknown> & { id: string };
const openSession = (key: string, where: "full" | "map" = "full"): Directive => ({
  id: directiveId(),
  op: "open",
  target: { type: "session", id: key },
  where,
});
const go = (section: string): Directive => ({ id: directiveId(), op: "go", section });

async function mockApp(page: Page): Promise<FakeAsk> {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route("**/api/**/draft", (r) =>
    r.fulfill({ json: { id: "d", text: "", attachments: [], updated_at: null } }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({
      json: { projects: [{ id: "p1", name: "proj", color: "#ffb000", archived: false }] },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/sessions?**", (r) =>
    r.fulfill({
      json: {
        sessions,
        next_offset: null,
        total: sessions.length,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({ json: { missions: [], total: 0, next_offset: null } }),
  );
  await page.route(/\/api\/templates(\?.*)?$/, (r) =>
    r.fulfill({ json: { templates: [], limits: TEMPLATE_LIMITS } }),
  );
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        onboarded: true,
        overview_expanded: ["project:p1"],
        projects_hidden: [],
        project_names: {},
        compose_default: "collapsed",
        pulse: { configured: true },
      },
    }),
  );
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "seq", n: 0 }));
    ws.send(Buffer.from(`\x1b[2J\x1b[H${"Ready terminal content\r\n".repeat(30)}`));
  });
  await mockRoster(page);
  const fake = await mockAskConversations(page);
  // HQ's stream, fed by the test: each message request gets a body that stays open.
  await page.addInitScript(() => {
    const real = window.fetch.bind(window);
    const held: { cid: string; turnId: string; text: string; ctl: ReadableStreamDefaultController }[] =
      [];
    (window as unknown as { __hqStreams: typeof held }).__hqStreams = held;
    window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const m = /\/api\/ask\/conversations\/([^/]+)\/message\/stream$/.exec(url.split("?")[0]);
      if (!m) return real(input, init);
      const body = JSON.parse(String(init?.body ?? "{}")) as { turn_id: string; text: string };
      let ctl!: ReadableStreamDefaultController;
      const stream = new ReadableStream({ start: (c) => void (ctl = c) });
      // As a real fetch: aborting the request ends the body, and nothing more can be read.
      init?.signal?.addEventListener("abort", () => {
        try {
          ctl.error(new DOMException("The operation was aborted.", "AbortError"));
        } catch {
          /* already closed */
        }
      });
      held.push({ cid: m[1], turnId: body.turn_id, text: body.text, ctl });
      return Promise.resolve(
        new Response(stream, { status: 200, headers: { "Content-Type": "application/x-ndjson" } }),
      );
    };
  });
  return fake;
}

interface Stream {
  cid: string;
  turnId: string;
  text: string;
}

/** The newest held stream (the turn the page just asked). */
async function stream(page: Page): Promise<Stream> {
  const read = () =>
    page.evaluate(() => {
      const held = (window as unknown as { __hqStreams: Stream[] }).__hqStreams;
      const s = held[held.length - 1];
      return s ? { cid: s.cid, turnId: s.turnId, text: s.text } : null;
    });
  await expect.poll(read).not.toBeNull();
  return (await read())!;
}

async function push(page: Page, line: Record<string, unknown>, close = false) {
  await page.evaluate(
    ([text, end]) => {
      type Held = { ctl: ReadableStreamDefaultController };
      const held = (window as unknown as { __hqStreams: Held[] }).__hqStreams;
      const { ctl } = held[held.length - 1];
      try {
        ctl.enqueue(new TextEncoder().encode(`${text as string}\n`));
        if (end) ctl.close();
      } catch {
        /* the page stopped watching this stream: the line reaches nobody */
      }
    },
    [JSON.stringify(line), close] as const,
  );
}

/** One live `screen` event, as the server sends it on the asking stream. */
const show = (page: Page, turnId: string, directive: Directive, title = "") =>
  push(page, { type: "screen", turn_id: turnId, directive, title });

/** Several live events in ONE chunk of the stream: all are handled before React renders. */
async function showTogether(page: Page, turnId: string, directives: Directive[], title = "") {
  await page.evaluate(
    (lines) => {
      type Held = { ctl: ReadableStreamDefaultController };
      const held = (window as unknown as { __hqStreams: Held[] }).__hqStreams;
      held[held.length - 1].ctl.enqueue(new TextEncoder().encode(lines.join("")));
    },
    directives.map(
      (directive) => `${JSON.stringify({ type: "screen", turn_id: turnId, directive, title })}\n`,
    ),
  );
}

/** Settle the turn: its last line, and the record a reload or another reader gets back. */
async function settle(
  page: Page,
  fake: FakeAsk,
  s: Stream,
  shown: { directive: Directive; title?: string }[],
  answer = "On screen, Commander.",
) {
  const turn = {
    turn_id: s.turnId,
    question: s.text,
    asked_at: now,
    status: "done",
    answer,
    detail: null,
    code: null,
    matches: [],
    mission_matches: [],
    tools: [],
    contact: "off",
    shows: shown.map((x) => ({ ...x.directive, title: x.title ?? "" })),
  } as unknown as FakeTurn;
  const conv = fake.conversations.find((c) => c.id === s.cid);
  if (conv) conv.turns = [...conv.turns.filter((t) => t.turn_id !== s.turnId), turn];
  await push(page, { type: "turn", status: 200, turn }, true);
}

async function ask(page: Page, text: string): Promise<Stream> {
  const panel = await openAsk(page);
  await panel.getByTestId("composer-input").fill(text);
  await panel.getByTestId("composer-input").press("Enter");
  return stream(page);
}

/** The lines under the newest answer. On a phone HQ is a drawer that gave way to what opened,
 *  so it is opened again first: the conversation is still there. */
async function lines(page: Page) {
  const panel = await openAsk(page);
  return panel.getByTestId("ask-turn").last().getByTestId("ask-shown");
}

const windowOf = (page: Page, key: string) => page.locator(`[data-session-window="${key}"]`);
const pathOf = (page: Page) => new URL(page.url()).pathname + new URL(page.url()).search;
const isPhone = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;

async function seedMap(page: Page, cap: number, open: string[] = [], minimized = false) {
  await page.addInitScript(
    ([c, keys, parked]) => {
      if (sessionStorage.getItem("seeded")) return;
      sessionStorage.setItem("seeded", "1");
      localStorage.setItem("tr-overview-window-cap", String(c));
      localStorage.setItem(
        "tr-overview-workspace",
        JSON.stringify(
          (keys as string[]).map((key, i) => ({
            key,
            engine: "claude",
            id: key.slice("claude:".length),
            title: key,
            x: 40 + i * 40,
            y: 60 + i * 40,
            w: 640,
            h: 420,
            z: i + 1,
            ...(parked ? { minimized: true } : {}),
          })),
        ),
      );
    },
    [cap, open, minimized] as const,
  );
}

test("a session opens full screen: the route changes, focus follows, the line says Shown", async ({
  page,
}) => {
  const fake = await mockApp(page);
  await page.goto(DASHBOARD_PATH);
  const s = await ask(page, "Pull up window session 1.");
  const d = openSession(KEYS[0]);
  await show(page, s.turnId, d, titleOf(KEYS[0]));
  await expect.poll(() => pathOf(page)).toBe(matchRoute(KEYS[0]));
  // Keyboard focus lands in the opened view, not in HQ.
  await expect
    .poll(() => page.evaluate(() => Boolean(document.activeElement?.closest("main"))))
    .toBe(true);
  if (isPhone(page)) {
    // The drawer covered the page: it closed when the directive navigated.
    await expect(page.getByTestId("ask-sidebar")).toHaveAttribute("data-open", "false");
  }
  await settle(page, fake, s, [{ directive: d, title: titleOf(KEYS[0]) }]);
  const line = await lines(page);
  await expect(line).toHaveCount(1);
  await expect(line).toHaveText(/^Shown: Window session 1 · full screenOpen$/);
  await expect(line).toHaveAttribute("data-kind", "shown");
  // The line is a quiet row with a link, never a card: Open is a real target on a phone.
  const open = line.getByTestId("ask-shown-open");
  const box = (await open.boundingBox())!;
  if (isPhone(page)) expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  else expect(Math.round(box.height)).toBe(32);
});

test("a session opens as a map window from a page where the map was not mounted; a phone falls back to full screen", async ({
  page,
}) => {
  const fake = await mockApp(page);
  await page.goto(DASHBOARD_PATH);
  const s = await ask(page, "Pull up window session 2 in the map.");
  const d = openSession(KEYS[1], "map");
  await show(page, s.turnId, d, titleOf(KEYS[1]));
  if (isPhone(page)) {
    await expect.poll(() => pathOf(page)).toBe(matchRoute(KEYS[1]));
    await settle(page, fake, s, [{ directive: d, title: titleOf(KEYS[1]) }]);
    await expect(await lines(page)).toHaveText(
      /^Shown: Window session 2 · full screen \(the map can't host a window here\)Open$/,
    );
    return;
  }
  // The request was queued before the map existed, and opens once it has mounted.
  await expect.poll(() => pathOf(page)).toBe(MAP_PATH);
  await expect(windowOf(page, KEYS[1])).toBeVisible();
  await expect(page.locator("[data-window-layer]")).toHaveAttribute("data-window-count", "1");
  // HQ adds no chrome to a window it opened: the same bar as any other window.
  await expect(windowOf(page, KEYS[1]).locator("[data-window-head]")).toHaveCount(1);
  // Focus is in the window that opened.
  await expect
    .poll(() =>
      page.evaluate(
        (key) => Boolean(document.activeElement?.closest(`[data-session-window="${key}"]`)),
        KEYS[1],
      ),
    )
    .toBe(true);
  await settle(page, fake, s, [{ directive: d, title: titleOf(KEYS[1]) }]);
  await expect(await lines(page)).toHaveText(/^Shown: Window session 2 · in the mapOpen$/);
});

test("a mission, an artifact and a section each open, and the line names each", async ({
  page,
}) => {
  const fake = await mockApp(page);
  await page.goto(DASHBOARD_PATH);
  const cases: [Directive, string, string, RegExp][] = [
    [
      { id: directiveId(), op: "open", target: { type: "mission", id: M1 }, where: "full" },
      "Release train",
      missionLink(M1),
      /^Shown: Release train · missionOpen$/,
    ],
    [
      { id: directiveId(), op: "open", target: { type: "artifact", id: A1 }, where: "full" },
      "Release notes",
      artifactPath(A1),
      /^Shown: Release notes · artifactOpen$/,
    ],
    [go("automations"), "", AUTOMATIONS_PATH, /^Shown: Automations · LibraryOpen$/],
    [
      go("settings.security"),
      "",
      settingsPath("security"),
      /^Shown: Security · SettingsOpen$/,
    ],
  ];
  for (const [directive, title, path, words] of cases) {
    const s = await ask(page, `Show me ${title || path}.`);
    await show(page, s.turnId, directive, title);
    // The console shape-checks `?m=` and settles the URL on the section: the pathname is the proof.
    await expect.poll(() => new URL(page.url()).pathname).toBe(path.split("?")[0]);
    await settle(page, fake, s, [{ directive, title }]);
    await expect(await lines(page)).toHaveText(words);
  }
});

test("four sessions asked for in one turn open four windows", async ({ page }) => {
  test.skip(isPhone(page), "window mode does not exist on a phone");
  const fake = await mockApp(page);
  await page.goto(DASHBOARD_PATH);
  const s = await ask(page, "Open the four sessions in the map.");
  const shown = KEYS.slice(0, 4).map((key) => ({
    directive: openSession(key, "map"),
    title: titleOf(key),
  }));
  for (const x of shown) await show(page, s.turnId, x.directive, x.title);
  await expect.poll(() => pathOf(page)).toBe(MAP_PATH);
  await expect(page.locator("[data-window-layer]")).toHaveAttribute("data-window-count", "4");
  for (const key of KEYS.slice(0, 4)) await expect(windowOf(page, key)).toBeAttached();
  await settle(page, fake, s, shown);
  const line = await lines(page);
  await expect(line).toHaveCount(1);
  await expect(line).toHaveText(/^Shown: 4 sessions · in the mapOpen$/);
});

test("a full map falls back to full screen and says why", async ({ page }) => {
  test.skip(isPhone(page), "window mode does not exist on a phone");
  const fake = await mockApp(page);
  await seedMap(page, 1, [KEYS[4]]);
  await page.goto(MAP_PATH);
  await expect(windowOf(page, KEYS[4])).toBeVisible();
  const s = await ask(page, "Pull up window session 1 in the map.");
  const d = openSession(KEYS[0], "map");
  await show(page, s.turnId, d, titleOf(KEYS[0]));
  await expect.poll(() => pathOf(page)).toBe(matchRoute(KEYS[0]));
  await settle(page, fake, s, [{ directive: d, title: titleOf(KEYS[0]) }]);
  await expect(await lines(page)).toHaveText(
    /^Shown: Window session 1 · full screen \(the map is full\)Open$/,
  );
});

test("four requests with room for two open two windows and say so; Open then opens ONE full screen", async ({
  page,
}) => {
  test.skip(isPhone(page), "window mode does not exist on a phone");
  const fake = await mockApp(page);
  await seedMap(page, 2);
  await page.goto(MAP_PATH);
  await expect(page.locator("[data-window-cap-up]")).toBeVisible();
  const s = await ask(page, "Open the four sessions in the map.");
  const shown = KEYS.slice(0, 4).map((key) => ({
    directive: openSession(key, "map"),
    title: titleOf(key),
  }));
  for (const x of shown) await show(page, s.turnId, x.directive, x.title);
  await expect(page.locator("[data-window-layer]")).toHaveAttribute("data-window-count", "2");
  await expect(windowOf(page, KEYS[0])).toBeAttached();
  await expect(windowOf(page, KEYS[1])).toBeAttached();
  // The other two were NOT opened full screen one after the other: the map is still on screen.
  expect(pathOf(page)).toBe(MAP_PATH);
  await settle(page, fake, s, shown);
  const line = await lines(page);
  await expect(line).toHaveCount(2);
  await expect(line.nth(0)).toHaveText(
    /^Shown: 2 of 4 sessions · in the map \(the map is full\)Open$/,
  );
  await expect(line.nth(1)).toHaveText(/^Asked to show: 2 more sessionsOpen$/);
  await expect(line.nth(1)).toHaveAttribute("data-kind", "asked");
  // Still no room: Open takes the first of the two full screen, and only that one.
  await line.nth(1).getByTestId("ask-shown-open").click();
  await expect.poll(() => pathOf(page)).toBe(matchRoute(KEYS[2]));
});

test("after a load away from the map, closing a saved window really closes it: the map does not restore it", async ({
  page,
}) => {
  test.skip(isPhone(page), "window mode does not exist on a phone");
  const fake = await mockApp(page);
  await seedMap(page, 8, [KEYS[3], KEYS[4]]);
  // The page loads on the dashboard: the two windows exist only as the saved layout.
  await page.goto(DASHBOARD_PATH);
  const s = await ask(page, "Close window session 5 in the map.");
  const d: Directive = { id: directiveId(), op: "map_close", key: KEYS[4] };
  await show(page, s.turnId, d, titleOf(KEYS[4]));
  await settle(page, fake, s, [{ directive: d, title: titleOf(KEYS[4]) }]);
  await expect(await lines(page)).toHaveText(/^Shown: Window session 5 · window closedOpen$/);
  expect(pathOf(page)).toBe(DASHBOARD_PATH); // closing a window is no reason to leave the page
  // Opening the map, by a full load at that: the closed window is not restored, the other is.
  await page.goto(MAP_PATH);
  await expect(windowOf(page, KEYS[3])).toBeVisible();
  await expect(page.locator("[data-window-layer]")).toHaveAttribute("data-window-count", "1");
  await expect(windowOf(page, KEYS[4])).toHaveCount(0);
});

test("an open and a close delivered together leave no window once the map has drained", async ({
  page,
}) => {
  test.skip(isPhone(page), "window mode does not exist on a phone");
  const fake = await mockApp(page);
  await seedMap(page, 8, [KEYS[3]]);
  await page.goto(MAP_PATH);
  await expect(windowOf(page, KEYS[3])).toBeVisible();
  const s = await ask(page, "Open window session 1 in the map, then close it again.");
  const opened = openSession(KEYS[0], "map");
  const closed: Directive = { id: directiveId(), op: "map_close", key: KEYS[0] };
  // ONE chunk of the stream: both events are handled before React renders in between.
  await page.evaluate(
    ([a, b]) => {
      type Held = { ctl: ReadableStreamDefaultController };
      const held = (window as unknown as { __hqStreams: Held[] }).__hqStreams;
      held[held.length - 1].ctl.enqueue(new TextEncoder().encode(`${a}\n${b}\n`));
    },
    [opened, closed].map((directive) =>
      JSON.stringify({ type: "screen", turn_id: s.turnId, directive, title: titleOf(KEYS[0]) }),
    ),
  );
  await settle(page, fake, s, [
    { directive: opened, title: titleOf(KEYS[0]) },
    { directive: closed, title: titleOf(KEYS[0]) },
  ]);
  const line = await lines(page);
  await expect(line).toHaveCount(1);
  await expect(line).toHaveText(/^Shown: Window session 1 · window closedOpen$/);
  await page.waitForTimeout(500); // long past a drain
  await expect(windowOf(page, KEYS[0])).toHaveCount(0);
  await expect(page.locator("[data-window-layer]")).toHaveAttribute("data-window-count", "1");
});

test("an open and an arrange delivered together tile the new window with the one already there", async ({
  page,
}) => {
  test.skip(isPhone(page), "window mode does not exist on a phone");
  const fake = await mockApp(page);
  await seedMap(page, 8, [KEYS[3]]);
  await page.goto(MAP_PATH);
  await expect(windowOf(page, KEYS[3])).toBeVisible();
  const s = await ask(page, "Open window session 1 in the map and arrange the windows.");
  const opened = openSession(KEYS[0], "map");
  const arranged: Directive = { id: directiveId(), op: "map_arrange" };
  await page.evaluate(
    ([a, b]) => {
      type Held = { ctl: ReadableStreamDefaultController };
      const held = (window as unknown as { __hqStreams: Held[] }).__hqStreams;
      held[held.length - 1].ctl.enqueue(new TextEncoder().encode(`${a}\n${b}\n`));
    },
    [opened, arranged].map((directive) =>
      JSON.stringify({ type: "screen", turn_id: s.turnId, directive, title: titleOf(KEYS[0]) }),
    ),
  );
  await expect(page.locator("[data-window-layer]")).toHaveAttribute("data-window-count", "2");
  // Arrange ran over BOTH windows: either they are tiled side by side, or the map had room for
  // one tile and parked the other in the tray. A window opened after the arrange would instead
  // sit cascaded on top of the seeded one, overlapping it.
  await expect
    .poll(async () => {
      const parked = await page.locator('[data-window-minimized="true"]').count();
      if (parked === 1) return true;
      const a = await windowOf(page, KEYS[0]).boundingBox();
      const b = await windowOf(page, KEYS[3]).boundingBox();
      if (!a || !b) return false;
      return (
        a.x + a.width <= b.x + 1 ||
        b.x + b.width <= a.x + 1 ||
        a.y + a.height <= b.y + 1 ||
        b.y + b.height <= a.y + 1
      );
    })
    .toBe(true);
  await settle(page, fake, s, [
    { directive: opened, title: titleOf(KEYS[0]) },
    { directive: arranged },
  ]);
  // One line for what the map ended up doing: both, or (the new window parked by the tiling)
  // only the arrange.
  await expect(await lines(page)).toHaveText(
    /^Shown: (2 items|the map · windows arranged)Open$/,
  );
});

test("at a full map, a close and an open delivered together replace the window", async ({
  page,
}) => {
  test.skip(isPhone(page), "window mode does not exist on a phone");
  const fake = await mockApp(page);
  await seedMap(page, 2, [KEYS[3], KEYS[4]]);
  await page.goto(MAP_PATH);
  await expect(page.locator("[data-window-layer]")).toHaveAttribute("data-window-count", "2");
  const s = await ask(page, "Close window session 5 and open window session 1 in the map.");
  const closed: Directive = { id: directiveId(), op: "map_close", key: KEYS[4] };
  const opened = openSession(KEYS[0], "map");
  await showTogether(page, s.turnId, [closed, opened]);
  // The close freed the slot the open was admitted against: a window, not a full-screen fallback.
  await expect(windowOf(page, KEYS[0])).toBeVisible();
  await expect(windowOf(page, KEYS[4])).toHaveCount(0);
  await expect(page.locator("[data-window-layer]")).toHaveAttribute("data-window-count", "2");
  expect(pathOf(page)).toBe(MAP_PATH);
  await settle(page, fake, s, [{ directive: closed }, { directive: opened }]);
  await expect(await lines(page)).toHaveText(/^Shown: 2 itemsOpen$/);
});

test("arrange with every window minimised settles, and says why nothing was arranged", async ({
  page,
}) => {
  test.skip(isPhone(page), "window mode does not exist on a phone");
  const fake = await mockApp(page);
  await seedMap(page, 8, [KEYS[3], KEYS[4]], true);
  await page.goto(MAP_PATH);
  await expect(page.locator('[data-window-minimized="true"]')).toHaveCount(2);
  const s = await ask(page, "Arrange the windows.");
  const d: Directive = { id: directiveId(), op: "map_arrange" };
  await show(page, s.turnId, d);
  await settle(page, fake, s, [{ directive: d }], "Nothing to arrange, Commander.");
  const line = await lines(page);
  await expect(line).toHaveText(/^Not opened: every window in the map is minimised$/);
  await expect(line).toHaveAttribute("data-kind", "held");
  await expect(page.locator('[data-window-minimized="true"]')).toHaveCount(2);
});

test("a reload does not run the directive again, and reads Asked to show with its link", async ({
  page,
}) => {
  const fake = await mockApp(page);
  await page.goto(DASHBOARD_PATH);
  const s = await ask(page, "Go to the automations.");
  const d = go("automations");
  await show(page, s.turnId, d);
  await expect.poll(() => pathOf(page)).toBe(AUTOMATIONS_PATH);
  await settle(page, fake, s, [{ directive: d }]);
  await expect(await lines(page)).toHaveText(/^Shown: Automations · LibraryOpen$/);

  await page.goto(DASHBOARD_PATH); // a full load: this page's memory of the outcome is gone
  const panel = await openAsk(page);
  await pickConversation(panel);
  const line = panel.getByTestId("ask-turn").last().getByTestId("ask-shown");
  await expect(line).toHaveText(/^Asked to show: Automations · LibraryOpen$/);
  await expect(line).toHaveAttribute("data-kind", "asked");
  // Reading the record back moved nothing.
  await page.waitForTimeout(400);
  expect(pathOf(page)).toBe(DASHBOARD_PATH);
  // The link is the Commander's own way in.
  await line.getByTestId("ask-shown-open").click();
  await expect.poll(() => pathOf(page)).toBe(AUTOMATIONS_PATH);
});

test("a second tab open on the same conversation does not move", async ({ page, context }) => {
  const fake = await mockApp(page);
  await page.goto(DASHBOARD_PATH);
  const s = await ask(page, "Go to the automations.");
  const d = go("automations");
  // The other reader: the same conversation, read from the server while the turn runs and after.
  const other = await context.newPage();
  const otherFake = await mockApp(other);
  otherFake.conversations = fake.conversations;
  await other.goto(DASHBOARD_PATH);

  await show(page, s.turnId, d);
  await expect.poll(() => pathOf(page)).toBe(AUTOMATIONS_PATH);
  await settle(page, fake, s, [{ directive: d }]);

  const panel = await openAsk(other);
  await pickConversation(panel);
  const line = panel.getByTestId("ask-turn").last().getByTestId("ask-shown");
  await expect(line).toHaveText(/^Asked to show: Automations · LibraryOpen$/);
  await other.waitForTimeout(400);
  expect(pathOf(other)).toBe(DASHBOARD_PATH);
});

test("a duplicate live event does nothing", async ({ page }) => {
  const fake = await mockApp(page);
  await page.goto(DASHBOARD_PATH);
  const s = await ask(page, "Go to the automations, then the templates.");
  const first = go("automations");
  const second = go("templates");
  await show(page, s.turnId, first);
  await expect.poll(() => pathOf(page)).toBe(AUTOMATIONS_PATH);
  await show(page, s.turnId, second);
  await expect.poll(() => pathOf(page)).toBe(TEMPLATES_PATH);
  // The first one again, as a reconnect's replay would deliver it: applied, it would go back.
  await show(page, s.turnId, first);
  await page.waitForTimeout(400);
  expect(pathOf(page)).toBe(TEMPLATES_PATH);
  await settle(page, fake, s, [{ directive: first }, { directive: second }]);
  await expect(await lines(page)).toHaveCount(1);
  await expect(await lines(page)).toHaveText(/^Shown: 2 itemsOpen$/);
});

test("an unsaved-changes prompt holds the navigation: Not opened, and a duplicate after the cancel does nothing", async ({
  page,
}) => {
  const fake = await mockApp(page);
  await page.goto(`${TEMPLATES_PATH}/new`);
  await page.getByLabel(/^name/i).fill("Half-written template");
  const here = pathOf(page);
  const s = await ask(page, "Go to the automations.");
  const d = go("automations");
  await show(page, s.turnId, d);
  // The page's own prompt, exactly as for a link: nothing moved, and the Commander is asked.
  const dialog = page.getByRole("dialog").filter({ hasText: /unsaved changes/i });
  await expect(dialog).toBeVisible();
  expect(pathOf(page)).toBe(here);
  await dialog.getByRole("button", { name: /keep editing/i }).click();
  await expect(dialog).toBeHidden();
  expect(pathOf(page)).toBe(here);
  await expect(page.getByLabel(/^name/i)).toHaveValue("Half-written template");

  // The same directive again: its id was consumed when it was held, so no prompt, no move.
  await show(page, s.turnId, d);
  await page.waitForTimeout(400);
  await expect(dialog).toBeHidden();
  expect(pathOf(page)).toBe(here);

  await settle(page, fake, s, [{ directive: d }], "Held, Commander.");
  const line = await lines(page);
  await expect(line).toHaveText(/^Not opened: you have unsaved changes on this pageOpen$/);
  await expect(line).toHaveAttribute("data-kind", "held");
});

test("a directive that navigates does not drop the ones behind it", async ({ page }) => {
  const fake = await mockApp(page);
  // HQ's own page: the first directive navigates AWAY from the console that is reading the stream.
  await page.goto(HQ_PATH);
  // A phone lands on the conversation list; New conversation is the thread.
  if (isPhone(page)) await page.getByTestId("hq-list").getByTestId("hq-new").click();
  await page.getByTestId("composer-input").fill("Show me the automations, then the map.");
  await page.getByTestId("composer-input").press("Enter");
  const s = await stream(page);
  const first = go("automations");
  await show(page, s.turnId, first);
  await expect.poll(() => pathOf(page)).toBe(AUTOMATIONS_PATH);
  await expect(page.getByTestId("composer-input")).toHaveCount(0); // HQ's page is gone
  // The one behind it arrives after the page that asked was unmounted, and still runs.
  const second = go("templates");
  await show(page, s.turnId, second);
  await expect.poll(() => pathOf(page)).toBe(TEMPLATES_PATH);
  await settle(page, fake, s, [{ directive: first }, { directive: second }]);
  // Both are on the turn's record (read back after a full load, so as requests).
  await page.goto(`${HQ_PATH}/c/${s.cid}`);
  await expect(page.getByTestId("ask-shown")).toHaveText(/^Asked to show: 2 itemsOpen$/);
});

test.describe("on a call", () => {
  test("a directive reaches the call's browser; on a phone the call minimises to its bar with Mute, Open and End", async ({
    page,
  }) => {
    const srv = await mockShell(page);
    const panel = await connect(page, srv);
    srv.connected[0] = true;
    say(srv, { t: "connected" });
    await expect(panel).toHaveAttribute("data-phase", "connected");
    const d = go("automations");
    say(srv, { t: "screen", turn_id: "t9", directive: d, title: "" });
    await expect.poll(() => pathOf(page)).toBe(AUTOMATIONS_PATH);
    if (!isPhone(page)) {
      // A wide screen: the call panel stays where it is, beside the page that opened.
      await expect(panel).toBeVisible();
      await expect(page.getByTestId("call-bar")).toHaveCount(0);
    } else {
      // A phone: the panel covered the page, so the call gave way to its on-call bar.
      const bar = page.getByTestId("call-bar");
      await expect(bar).toBeVisible();
      await expect(page.getByTestId("call-panel")).toHaveCount(0);
      for (const id of ["call-bar-mute", "call-bar-open", "call-bar-end"]) {
        const box = (await bar.getByTestId(id).boundingBox())!;
        expect(box.height, id).toBeGreaterThanOrEqual(TOUCH_FLOOR);
        expect(box.width, id).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      }
      // End is the filled danger plate; Mute works from the bar; Open brings the panel back.
      await bar.getByTestId("call-bar-mute").click();
      await expect
        .poll(() => srv.texts[0].map((t) => JSON.parse(t).t as string))
        .toContain("mute");
      await bar.getByTestId("call-bar-open").click();
      await expect(page.getByTestId("call-panel")).toBeVisible();
      await expect(page.getByTestId("call-bar")).toHaveCount(0);
    }
  });

  test("a duplicate on the call socket is applied once", async ({ page }) => {
    const srv = await mockShell(page);
    const panel = await connect(page, srv);
    srv.connected[0] = true;
    say(srv, { t: "connected" });
    await expect(panel).toHaveAttribute("data-phase", "connected");
    const first = go("automations");
    const second = go("templates");
    say(srv, { t: "screen", turn_id: "t9", directive: first });
    await expect.poll(() => pathOf(page)).toBe(AUTOMATIONS_PATH);
    say(srv, { t: "screen", turn_id: "t9", directive: second });
    await expect.poll(() => pathOf(page)).toBe(TEMPLATES_PATH);
    say(srv, { t: "screen", turn_id: "t9", directive: first });
    await page.waitForTimeout(400);
    expect(pathOf(page)).toBe(TEMPLATES_PATH);
  });
});
