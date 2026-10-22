import { expect, test, type Page, type Route } from "@playwright/test";
import { ROSTER, RUNTIME_HINTS_KEY, runtimeHintsValue } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

/** API-session links open straight into the conversation (#1502).
 *
 * A link to an API session (`/s/<api-engine>/<id>`) opened cold — a notification click, a link
 * from another tab — used to mount a TERMINAL while the engine roster was still loading. Its socket
 * was refused 4404 and the page said "session not found or ended" until a reload. The page now
 * draws its pane from the runtime the last loaded roster recorded in this browser, waits with a
 * neutral "Opening…" when it has never seen a roster, says "Couldn't load the agent list. Retry"
 * after 8 s of no roster, re-gates on the server's 4415 "not a terminal session", and masks a 4404
 * only while the row lookup for that exact key is still pending.
 *
 * Every test starts from an EMPTY storage state (the suite default carries the runtime map of a
 * returning browser) and seeds exactly the hint it is about. Engine ids come from the roster
 * fixture by runtime, never from a literal. Each case watches every DOM mutation, so a not-found
 * text that was on screen for a single frame still fails it.
 */

type Row = (typeof ROSTER.engines)[number];
const API_ENGINE = ROSTER.engines.find(
  (e) => e.runtime === "api" && typeof (e.api as { source?: unknown } | undefined)?.source === "string",
) as Row & { api: { source: string } };
const API = API_ENGINE.id;
/** The console engine whose native history the API engine drives: a `pty` engine. */
const PTY = API_ENGINE.api.source;
/** An engine BattleLab talks to as a chat client. */
const CHAT = ROSTER.engines.find((e) => e.runtime === "chat")!.id;
const ID = "6c51dd58-9155-47b0-aafa-2ccf0de04908";
const NATIVE = "24c5e209-0000-4000-8000-000000000001";
const CWD = "/home/u/proj";
const NOW = Math.floor(Date.now() / 1000);

/** Texts that must never appear while anything is still being found out. */
const NOT_FOUND_TEXTS = [
  "session not found or ended",
  "This conversation does not exist",
  "This session was not found",
];
const CHAT_LOAD_ERROR = "Couldn’t load this conversation.";
const OPENING_TEXT = "Opening this session…";

test.use({ storageState: { cookies: [], origins: [] } });
test.describe.configure({ timeout: 60_000 });

interface Ctl {
  /** How the roster answers. `hang` never answers within a test. */
  roster: "ok" | "fail" | "hang";
  rosterDelayMs: number;
  /** The ws/term close code, or null to leave the socket open. */
  wsClose: number | null;
}

function row(engine: string, id: string, extra: Record<string, unknown> = {}) {
  return {
    id: `${engine}:${id}`,
    engine,
    uuid: id,
    short_uuid: id.slice(0, 8),
    cwd: CWD,
    project: { kind: "folder", id: CWD, name: CWD },
    last_mtime: NOW - 600,
    created_at: NOW - 900,
    title: "the session",
    first_user_message: "hello",
    sticky: false,
    archived: false,
    ...extra,
  };
}

function snapshot(engine: string, id: string) {
  return {
    session_key: `${engine}:${id}`,
    revision: 1,
    event_cursor: 1,
    cwd: CWD,
    state: "idle",
    active_turn: null,
    model_requested: null,
    model_effective: null,
    turns: [],
    omitted_turns: 0,
    pending_requests: [],
    native: { native_id: NATIVE, worker: "w1", background_active: false },
    read_only: null,
  };
}

/** `/api/sessions/<engine>:<id>` exactly — the colon may arrive encoded. */
const lookupPath = (engine: string, id: string) =>
  new RegExp(`/api/sessions/${engine}(?::|%3A)${id}$`);
const structuredPath = (engine: string, id: string) =>
  new RegExp(`/api/structured/sessions/${engine}(?::|%3A)${id}(/[a-z]+)?(\\?.*)?$`);

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function fulfilSafely(route: Route, body: Parameters<Route["fulfill"]>[0]) {
  try {
    await route.fulfill(body);
  } catch {
    /* the page closed first (a `hang` roster outliving its test) */
  }
}

/** The app around one session page. Returns the terminal sockets the page opened. */
async function setup(
  page: Page,
  ctl: Ctl,
  opts: { hints?: Record<string, string> } = {},
): Promise<{ sockets: string[] }> {
  // What a fresh profile holds, plus the hint under test (null = a never-visited browser).
  await page.addInitScript(
    ([key, value]) => {
      localStorage.setItem("battlelab.linkOpenMode", "fullscreen");
      if (value) localStorage.setItem(key, value);
      else localStorage.removeItem(key);
    },
    [RUNTIME_HINTS_KEY, opts.hints ? runtimeHintsValue(opts.hints) : null] as const,
  );
  // Record every not-found (and Opening) text that ever reaches the DOM, however briefly.
  await page.addInitScript((needles: string[]) => {
    const w = window as unknown as { __seen: string[] };
    w.__seen = [];
    const watch = () => {
      const text = document.body?.innerText ?? "";
      for (const n of needles) if (text.includes(n) && !w.__seen.includes(n)) w.__seen.push(n);
    };
    new MutationObserver(watch).observe(document, {
      subtree: true,
      childList: true,
      characterData: true,
    });
  }, [...NOT_FOUND_TEXTS, OPENING_TEXT, CHAT_LOAD_ERROR]);

  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [PTY, API],
        terminal_backend: "ws",
        auth_mode: "none",
        default_project: CWD,
        overview_expanded: [],
        projects_hidden: [],
      },
    }),
  );
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/engines", async (r) => {
    if (ctl.roster === "hang") return; // never answered: the request stays pending
    if (ctl.rosterDelayMs) await sleep(ctl.rosterDelayMs);
    if (ctl.roster === "fail")
      return fulfilSafely(r, { status: 500, json: { detail: "agent list unavailable" } });
    return fulfilSafely(r, { json: { engines: ROSTER.engines, problems: [] } });
  });
  // The API session itself: its row and its structured snapshot.
  await page.route(lookupPath(API, ID), (r) => r.fulfill({ json: row(API, ID) }));
  await page.route(structuredPath(API, ID), (r) => {
    const snap = snapshot(API, ID);
    if (r.request().url().includes("/events"))
      return r.fulfill({
        json: { session_key: snap.session_key, revision: 1, next_cursor: 1, events: [] },
      });
    return r.fulfill({ json: snap });
  });

  const sockets: string[] = [];
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    sockets.push(ws.url());
    if (ctl.wsClose !== null) void ws.close({ code: ctl.wsClose, reason: "" });
  });
  return { sockets };
}

async function everShown(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as { __seen: string[] }).__seen);
}
/** The not-found texts that were ever on screen. */
async function seen(page: Page): Promise<string[]> {
  return (await everShown(page)).filter((t) => NOT_FOUND_TEXTS.includes(t));
}
/** Was the neutral "Opening…" panel ever on screen? */
async function sawOpening(page: Page): Promise<boolean> {
  return (await everShown(page)).includes(OPENING_TEXT);
}

const opening = (page: Page) => page.getByText("Opening this session…");
const unavailable = (page: Page) => page.getByRole("alert").filter({ hasText: "Couldn’t load the agent list." });

test("a cold link to an API session with a slow roster: Opening…, then the conversation — no terminal, no 'not found'", async ({
  page,
}) => {
  const { sockets } = await setup(page, { roster: "ok", rosterDelayMs: 2_000, wsClose: 4404 });
  await page.goto(`/s/${API}/${ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await expect(page.locator(".xterm")).toHaveCount(0);
  expect(await seen(page)).toEqual([]);
  expect(sockets).toEqual([]);
  expect(await sawOpening(page)).toBe(true);
  // …and the roster it loaded is now this browser's hint for the next cold load.
  const stored = await page.evaluate((k) => localStorage.getItem(k), RUNTIME_HINTS_KEY);
  expect(JSON.parse(stored ?? "{}").runtimes?.[API]).toBe("api");
});

test("a cached api hint draws the conversation at once — no terminal socket, no roster wait", async ({
  page,
}) => {
  const { sockets } = await setup(
    page,
    { roster: "hang", rosterDelayMs: 0, wsClose: 4404 },
    { hints: { [API]: "api", [PTY]: "pty" } },
  );
  await page.goto(`/s/${API}/${ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await expect(page.getByTestId("structured-worker")).toBeVisible();
  await expect(page.locator(".xterm")).toHaveCount(0);
  expect(sockets).toEqual([]);
  expect(await seen(page)).toEqual([]);
});

test("a roster that keeps failing on a first visit: Retry within 8 s, and Retry with a working roster opens the pane", async ({
  page,
}) => {
  const ctl: Ctl = { roster: "fail", rosterDelayMs: 0, wsClose: 4404 };
  const { sockets } = await setup(page, ctl);
  await page.goto(`/s/${API}/${ID}`);
  await expect(opening(page)).toBeVisible();
  await expect(unavailable(page)).toBeVisible({ timeout: 10_000 });
  await expect(unavailable(page)).toContainText("Nothing was started");
  const retry = page.getByRole("button", { name: "Retry" });
  const box = await retry.boundingBox();
  expect(box?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  ctl.roster = "ok";
  await retry.click();
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await expect(page.locator(".xterm")).toHaveCount(0);
  expect(sockets).toEqual([]);
  expect(await seen(page)).toEqual([]);
});

test("a stale pty hint for an API engine: the server's 4415 re-gates to Opening…, then the conversation", async ({
  page,
}) => {
  const { sockets } = await setup(
    page,
    { roster: "ok", rosterDelayMs: 4_000, wsClose: 4415 },
    { hints: { [API]: "pty" } },
  );
  await page.goto(`/s/${API}/${ID}`);
  await expect.poll(() => sockets.length).toBe(1);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await expect(page.locator(".xterm")).toHaveCount(0);
  expect(await seen(page)).toEqual([]);
  expect(sockets).toHaveLength(1); // refused once, never retried
  expect(await sawOpening(page)).toBe(true);
});

test("a stale pty hint with no roster to be had: 4415 → Opening… → Retry within 8 s", async ({
  page,
}) => {
  const { sockets } = await setup(
    page,
    { roster: "fail", rosterDelayMs: 0, wsClose: 4415 },
    { hints: { [API]: "pty" } },
  );
  await page.goto(`/s/${API}/${ID}`);
  await expect.poll(() => sockets.length).toBe(1);
  await expect(unavailable(page)).toBeVisible({ timeout: 10_000 });
  expect(sockets).toHaveLength(1);
  expect(await seen(page)).toEqual([]);
});

test.describe("a stale api hint for an engine that is a terminal engine now", () => {
  async function staleApi(page: Page, roster: Ctl["roster"]) {
    const ctl = await setup(
      page,
      { roster, rosterDelayMs: roster === "ok" ? 4_000 : 0, wsClose: null },
      { hints: { [PTY]: "api" } },
    );
    // The structured read for a console session misses: provisional until the roster confirms.
    await page.route(structuredPath(PTY, ID), (r) =>
      r.fulfill({ status: 404, json: { detail: "no such structured session" } }),
    );
    await page.route(lookupPath(PTY, ID), (r) => r.fulfill({ json: row(PTY, ID) }));
    return ctl;
  }

  test("delayed roster: the 404 reads Opening…, then the terminal", async ({ page }) => {
    const { sockets } = await staleApi(page, "ok");
    await page.goto(`/s/${PTY}/${ID}`);
    await expect(page.locator(".xterm")).toBeVisible();
    await expect.poll(() => sockets.length).toBe(1);
    expect(await seen(page)).toEqual([]);
    expect(await sawOpening(page)).toBe(true);
  });

  test("failing roster: Retry within 8 s, never 'does not exist'", async ({ page }) => {
    await staleApi(page, "fail");
    await page.goto(`/s/${PTY}/${ID}`);
    await expect(unavailable(page)).toBeVisible({ timeout: 10_000 });
    expect(await seen(page)).toEqual([]);
  });
});

test("a native-id alias link: Opening… while it resolves, then the owner — URL replaced", async ({
  page,
}) => {
  const { sockets } = await setup(
    page,
    { roster: "ok", rosterDelayMs: 0, wsClose: 4404 },
    { hints: { [PTY]: "pty", [API]: "api" } },
  );
  // The native key's lookup is slow, and answers with the OWNER, declared as an alias.
  await page.route(lookupPath(PTY, NATIVE), async (r) => {
    await sleep(2_500);
    await fulfilSafely(r, { json: { ...row(API, ID), resolved_from: `${PTY}:${NATIVE}` } });
  });
  await page.goto("/");
  await expect(page.locator("#root")).toBeVisible();
  await page.goto(`/s/${PTY}/${NATIVE}`);
  // The terminal was refused at once; the lookup has not answered: not a verdict yet.
  await expect.poll(() => sockets.length).toBeGreaterThanOrEqual(1);
  await expect(page).toHaveURL(new RegExp(`/s/${API}/${ID}$`));
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await expect(page.locator(".xterm")).toHaveCount(0);
  expect(await seen(page)).toEqual([]);
  expect(await sawOpening(page)).toBe(true);
  await page.goBack();
  await expect(page).not.toHaveURL(new RegExp(`/s/${PTY}/`));
});

// Hermes on #1522: an alias lookup slower than the 8 s give-up bound. When it finally resolves to
// the owner, the frame before the URL is replaced must not show the terminal's 4404 text.
test("a native-id alias that resolves after the give-up bound never flashes 'not found'", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const { sockets } = await setup(
    page,
    { roster: "ok", rosterDelayMs: 0, wsClose: 4404 },
    { hints: { [PTY]: "pty", [API]: "api" } },
  );
  // Resolves well past the 8 s bound: the chip must have a window wide enough to be seen on a
  // loaded runner (9.5 s left 1.5 s and failed CI under load, #1512).
  await page.route(lookupPath(PTY, NATIVE), async (r) => {
    await sleep(16_000);
    await fulfilSafely(r, { json: { ...row(API, ID), resolved_from: `${PTY}:${NATIVE}` } });
  });
  await page.goto(`/s/${PTY}/${NATIVE}`);
  await expect.poll(() => sockets.length).toBeGreaterThanOrEqual(1);
  // Past the bound the chip says it could not check — honest, and not "not found".
  await expect(page.getByText("couldn’t check this session — retrying")).toBeVisible({
    timeout: 12_000,
  });
  await expect(page).toHaveURL(new RegExp(`/s/${API}/${ID}$`), { timeout: 25_000 });
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  expect(await seen(page)).toEqual([]);
});

test.describe("stale hints between chat and api", () => {
  /** A chat-runtime session's reads, for the chat pane once the roster says so. */
  async function chatSession(page: Page, engine: string) {
    await page.route(new RegExp(`/api/chat/${engine}(?::|%3A)${ID}$`), (r) =>
      r.fulfill({ json: { session_id: ID, cwd: CWD, created_at: 0, turns: [], in_flight: null } }),
    );
    await page.route(`**/api/agents/${engine}/endpoint`, (r) =>
      r.fulfill({
        json: {
          base_url: "https://llm.example.lan/v1",
          model: "qwen3-coder",
          api_key_set: true,
          context_window: 32768,
          max_output_tokens: 4096,
          request_timeout: null,
          configured: true,
          tools: "none",
        },
      }),
    );
  }

  for (const roster of ["ok", "fail"] as const) {
    test(`a cached chat hint for an API engine (${roster === "ok" ? "delayed" : "failing"} roster)`, async ({
      page,
    }) => {
      const { sockets } = await setup(
        page,
        { roster, rosterDelayMs: roster === "ok" ? 4_000 : 0, wsClose: 4404 },
        { hints: { [API]: "chat" } },
      );
      // The chat route refuses a session that is not a chat session.
      await page.route(new RegExp(`/api/chat/${API}(?::|%3A)${ID}$`), (r) =>
        r.fulfill({ status: 409, json: { detail: "not a chat session" } }),
      );
      await page.goto(`/s/${API}/${ID}`);
      if (roster === "ok") {
        await expect(page.getByTestId("structured-pane")).toBeVisible();
        expect(await sawOpening(page)).toBe(true);
      } else {
        await expect(unavailable(page)).toBeVisible({ timeout: 10_000 });
      }
      await expect(page.getByText("Couldn’t load this conversation.")).toHaveCount(0);
      expect(await everShown(page)).not.toContain(CHAT_LOAD_ERROR);
      expect(sockets).toEqual([]);
      expect(await seen(page)).toEqual([]);
    });

    test(`a cached api hint for a chat engine (${roster === "ok" ? "delayed" : "failing"} roster)`, async ({
      page,
    }) => {
      const { sockets } = await setup(
        page,
        { roster, rosterDelayMs: roster === "ok" ? 4_000 : 0, wsClose: 4404 },
        { hints: { [CHAT]: "api" } },
      );
      await page.route(structuredPath(CHAT, ID), (r) =>
        r.fulfill({ status: 404, json: { detail: "no such structured session" } }),
      );
      await chatSession(page, CHAT);
      await page.goto(`/s/${CHAT}/${ID}`);
      if (roster === "ok") {
        await expect(page.getByTestId("chat-pane")).toBeVisible();
        expect(await sawOpening(page)).toBe(true);
      } else {
        await expect(unavailable(page)).toBeVisible({ timeout: 10_000 });
      }
      expect(sockets).toEqual([]);
      expect(await seen(page)).toEqual([]);
    });
  }
});

test("a truly missing id: the settled not-found result, once the lookup has answered", async ({
  page,
}) => {
  await setup(
    page,
    { roster: "ok", rosterDelayMs: 0, wsClose: 4404 },
    { hints: { [PTY]: "pty", [API]: "api" } },
  );
  // Slow enough that the terminal's 4404 reliably lands while the lookup is still pending — the
  // window this test is about. At 1.5 s a loaded runner could settle the lookup first, so the
  // interim "Opening…" was never needed and never drawn (#1512's CI).
  await page.route(lookupPath(PTY, NATIVE), async (r) => {
    await sleep(5_000);
    await fulfilSafely(r, { status: 404, json: { detail: "no such session" } });
  });
  await page.goto(`/s/${PTY}/${NATIVE}`);
  await expect(page.getByText("This session was not found.")).toBeVisible({ timeout: 15_000 });
  const back = page.getByRole("link", { name: /Back to Sessions/ });
  await expect(back).toBeVisible();
  expect((await back.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  // The interim chip never said "not found" before the lookup settled; it said Opening….
  expect(await seen(page)).toEqual(["This session was not found"]);
  expect(await sawOpening(page)).toBe(true);
});

test("a cached pty hint mounts the terminal without waiting for the roster (control)", async ({
  page,
}) => {
  const { sockets } = await setup(
    page,
    { roster: "hang", rosterDelayMs: 0, wsClose: null },
    { hints: { [PTY]: "pty", [API]: "api" } },
  );
  await page.route(lookupPath(PTY, ID), (r) => r.fulfill({ json: row(PTY, ID) }));
  await page.goto(`/s/${PTY}/${ID}`);
  await expect(page.locator(".xterm")).toBeVisible();
  await expect.poll(() => sockets.length).toBe(1);
  await expect(opening(page)).toHaveCount(0);
});
