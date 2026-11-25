import { expect, test, type Page, type Request, type WebSocketRoute } from "@playwright/test";
import { mockRoster, ROSTER } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

/** An archived session is frozen (#2064): no processes, and nothing may start it until it is
 *  unarchived. Opening one shows its history read-only with the ARCHIVED · READ-ONLY strip where
 *  the composer was; UNARCHIVE (one tap, a 44 px target) starts nothing and brings the composer
 *  back only once the session row reads active; a failed unarchive keeps it read-only with RETRY.
 *  A terminal session that is archived gets no terminal socket — and a 4410 refusal is never
 *  reconnected.
 *
 *  The server is mocked by route: the row (`GET /api/sessions/<key>`) is a small state machine the
 *  unarchive route flips, and every request is recorded so the spec can prove that nothing posted a
 *  turn or a start and that no terminal socket opened. Desktop and mobile, both themes. */

const API = (ROSTER.engines.find((e) => e.runtime === "api" && e.id === "codex-api") ??
  ROSTER.engines.find((e) => e.runtime === "api"))!.id;
const PTY = ROSTER.engines.find((e) => e.runtime === "pty" && e.id === "claude")!.id;
const API_ID = "7d1e0c2a-5b3f-4e61-9a8c-2f4e6d8b0a11";
const PTY_ID = "8e2f1d3b-6c4a-4f72-8b9d-3a5f7e9c1b22";
const CWD = "/home/u/proj";
const NOW = Math.floor(Date.now() / 1000);
const MISSION_HELD = "this session is held by a mission";

test.describe.configure({ timeout: 60_000 });

function row(engine: string, id: string, archived: boolean) {
  return {
    id: `${engine}:${id}`,
    engine,
    uuid: id,
    short_uuid: id.slice(0, 8),
    cwd: CWD,
    project: { kind: "folder", id: CWD, name: CWD },
    last_mtime: NOW - 120,
    created_at: NOW - 900,
    title: "the archived session",
    first_user_message: "so you work on this, right?",
    sticky: false,
    archived,
  };
}

function snapshot(engine: string, id: string) {
  return {
    session_key: `${engine}:${id}`,
    revision: 3,
    event_cursor: 3,
    cwd: CWD,
    state: "idle",
    active_turn: null,
    model_requested: null,
    model_effective: null,
    turns: [
      {
        turn_id: "t-1",
        operation_id: "t-1",
        state: "completed",
        text: "so you work on this, right?",
        reply: "Yes. This session holds the claim on the issue.",
        text_truncated: false,
        reply_truncated: false,
        reason: null,
        tools: [],
        tools_truncated: false,
      },
    ],
    omitted_turns: 0,
    pending_requests: [],
    read_only: null,
  };
}

const keyPath = (engine: string, id: string, tail = "") =>
  new RegExp(`/api/sessions/${engine}(?::|%3A)${id}${tail}$`);
const structuredPath = (engine: string, id: string) =>
  new RegExp(`/api/structured/sessions/${engine}(?::|%3A)${id}(/[a-z-]+)?(\\?.*)?$`);

interface Ctl {
  /** Each session's archived flag, as its row reads. */
  archived: Record<string, boolean>;
  /** How the unarchive route answers. */
  unarchive: "ok" | "fail";
  /** Held until the test releases it, so the in-flight state can be seen. */
  gate: Promise<void> | null;
}

async function setup(page: Page, theme: "dark" | "light", ctl: Ctl) {
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
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
        theme,
      },
    }),
  );
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await mockRoster(page);
  for (const [engine, id] of [
    [API, API_ID],
    [PTY, PTY_ID],
  ] as const) {
    const key = `${engine}:${id}`;
    await page.route(keyPath(engine, id), (r) =>
      r.fulfill({ json: row(engine, id, ctl.archived[key]) }),
    );
    await page.route(keyPath(engine, id, "/unarchive"), async (r) => {
      if (r.request().method() !== "POST") return r.fulfill({ status: 405, json: {} });
      if (ctl.gate) await ctl.gate;
      if (ctl.unarchive === "fail")
        return r.fulfill({ status: 409, json: { detail: MISSION_HELD } });
      ctl.archived[key] = false;
      return r.fulfill({ json: { id: key, archived: false } });
    });
    await page.route(keyPath(engine, id, "/draft"), (r) =>
      r.fulfill({ json: { id: key, text: "", attachments: [], updated_at: null, has_draft: false } }),
    );
  }
  await page.route(structuredPath(API, API_ID), (r) => {
    const snap = snapshot(API, API_ID);
    if (r.request().method() !== "GET")
      return r.fulfill({ status: 409, json: { detail: "this session is archived — unarchive it to continue" } });
    if (r.request().url().includes("/events"))
      return r.fulfill({ json: { session_key: snap.session_key, revision: 3, next_cursor: 3, events: [] } });
    return r.fulfill({ json: snap });
  });

  // Everything that could start the session: a structured POST (turns, start, send-now…) and any
  // terminal socket.
  const starts: string[] = [];
  page.on("request", (req: Request) => {
    if (req.method() !== "GET" && /\/api\/structured\//.test(req.url()))
      starts.push(`${req.method()} ${new URL(req.url()).pathname}`);
  });
  const sockets: string[] = [];
  const input: string[] = [];
  let activeSocket: WebSocketRoute | undefined;
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    sockets.push(ws.url());
    // What the server does with an archived session's socket (#2064): 4410, terminal.
    if (ctl.archived[`${PTY}:${PTY_ID}`]) {
      void ws.close({ code: 4410, reason: "this session is archived" });
      return;
    }
    activeSocket = ws;
    ws.onMessage((data) => {
      const frame = JSON.parse(String(data));
      if (frame.t === "i") input.push(frame.d);
    });
  });
  return {
    starts, sockets, input,
    output: (text: string) => {
      if (!activeSocket) throw new Error("terminal has not started");
      activeSocket.send(Buffer.from(text));
    },
  };
}

/** Client-side navigation (react-router listens to popstate): the app, its stores and their cached
 *  rows stay; only the route changes. */
async function navigate(page: Page, path: string) {
  await page.evaluate((p) => {
    window.history.pushState({}, "", p);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, path);
}

const strip = (page: Page) => page.getByTestId("archived-strip");
const unarchive = (page: Page) => page.getByTestId("archived-unarchive");
const composer = (page: Page) => page.getByRole("textbox", { name: /^Message / });

for (const theme of ["dark", "light"] as const) {
  test(`${theme}: an archived API session shows the history read-only with the strip, and starts nothing`, async ({
    page,
  }) => {
    const ctl: Ctl = { archived: { [`${API}:${API_ID}`]: true }, unarchive: "ok", gate: null };
    const { starts, sockets } = await setup(page, theme, ctl);
    await page.goto(`/s/${API}/${API_ID}`);
    await expect(strip(page)).toBeVisible();
    await expect(strip(page)).toContainText(/archived · read-only/i);
    await expect(page.getByText("Yes. This session holds the claim on the issue.")).toBeVisible();
    await expect(composer(page)).toHaveCount(0);
    await expect(page.getByTestId("structured-send")).toHaveCount(0);
    await expect(unarchive(page)).toBeFocused();
    const box = await unarchive(page).boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    // Let the pane's polls run: still nothing posted, no terminal socket.
    await page.waitForTimeout(1_500);
    expect(starts).toEqual([]);
    expect(sockets).toEqual([]);
  });
}

test("UNARCHIVE: busy, then the composer once the row reads active — the unarchive starts nothing", async ({
  page,
}) => {
  let release!: () => void;
  const ctl: Ctl = {
    archived: { [`${API}:${API_ID}`]: true },
    unarchive: "ok",
    gate: new Promise<void>((r) => (release = r)),
  };
  const { starts, sockets } = await setup(page, "dark", ctl);
  await page.goto(`/s/${API}/${API_ID}`);
  await expect(unarchive(page)).toBeVisible();
  await unarchive(page).click();
  await expect(unarchive(page)).toHaveText(/unarchiving…/i);
  await expect(unarchive(page)).toBeDisabled();
  await expect(composer(page)).toHaveCount(0);
  release();
  await expect(composer(page)).toBeVisible();
  await expect(strip(page)).toHaveCount(0);
  expect(ctl.archived[`${API}:${API_ID}`]).toBe(false);
  await page.waitForTimeout(1_000);
  expect(starts).toEqual([]);
  expect(sockets).toEqual([]);
});

test("a failed unarchive keeps the session read-only and offers RETRY with the reason", async ({
  page,
}) => {
  const ctl: Ctl = { archived: { [`${API}:${API_ID}`]: true }, unarchive: "fail", gate: null };
  const { starts } = await setup(page, "dark", ctl);
  await page.goto(`/s/${API}/${API_ID}`);
  await unarchive(page).click();
  await expect(strip(page).getByRole("alert")).toContainText(
    `Couldn’t unarchive: ${MISSION_HELD}. Still archived.`,
  );
  await expect(unarchive(page)).toHaveText(/retry/i);
  await expect(unarchive(page)).toBeEnabled();
  await expect(composer(page)).toHaveCount(0);
  expect(starts).toEqual([]);
});

for (const fresh of [false, true]) {
  test(`terminal UNARCHIVE stays stopped until Send, then holds the first message until boot is ready (fresh=${fresh})`, async ({ page }) => {
    const ctl: Ctl = {
      archived: { [`${PTY}:${PTY_ID}`]: true }, unarchive: "ok", gate: null,
    };
    const { sockets, input, output } = await setup(page, "dark", ctl);
    if (fresh) {
      // A pane originally opened by Start can still carry its launch state when later archived.
      // Model that router state: after Unarchive it must resume the existing id, never send new=1.
      await page.addInitScript((cwd) => {
        history.replaceState({ ...history.state, usr: { fresh: { cwd, bypass: true } } }, "");
      }, CWD);
    }
    await page.goto(`/s/${PTY}/${PTY_ID}`);
    await expect(strip(page)).toBeVisible();
    const cold = sockets.length;
    await unarchive(page).click();
    await expect(strip(page)).toHaveCount(0);
    // Wake events must not turn Unarchive into a launch either.
    await page.evaluate(() => {
      window.dispatchEvent(new Event("online"));
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await page.waitForTimeout(1_000);
    expect(sockets.length).toBe(cold);
    await expect(page.locator(".xterm")).toHaveCount(0);
    await expect(page.getByPlaceholder(/type here/i)).toBeVisible();
    await page.getByPlaceholder(/type here/i).fill("resume with this message");
    await page.getByRole("button", { name: /^send/i }).click();
    await expect.poll(() => sockets.length).toBe(cold + 1);
    expect(new URL(sockets.at(-1)!).searchParams.has("new")).toBe(false);
    await expect(page.getByText(/waiting for agent/i)).toBeVisible();
    await page.waitForTimeout(500);
    expect(input).toEqual([]);
    output("\x1b[?2004hReady\r\n❯ ");
    await expect.poll(() => input).toEqual([
      "\x01\x0b", "\x1b[200~resume with this message\x1b[201~", "\r",
    ]);
    await expect(page.getByPlaceholder(/type here/i)).toHaveValue("");
    expect(sockets.length).toBe(cold + 1);
  });
}

test("an archived terminal session: no terminal, the strip — a 4410 is never reconnected, and a known-archived row opens no socket", async ({
  page,
}) => {
  const ctl: Ctl = {
    archived: { [`${PTY}:${PTY_ID}`]: true, [`${API}:${API_ID}`]: true },
    unarchive: "ok",
    gate: null,
  };
  const { sockets } = await setup(page, "dark", ctl);
  await page.goto(`/s/${PTY}/${PTY_ID}`);
  await expect(page.getByTestId("term-archived")).toHaveText(
    "Archived — read-only. No terminal is running.",
  );
  await expect(strip(page)).toBeVisible();
  await expect(page.locator(".xterm")).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: /message|compose/i })).toHaveCount(0);
  const box = await unarchive(page).boundingBox();
  expect(box?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  // A cold link may race its row lookup with one socket; the server answers it 4410, and the pane
  // never asks again — no reconnect loop, however long it sits there.
  await page.waitForTimeout(3_000);
  expect(sockets.length).toBeLessThanOrEqual(1);
  const cold = sockets.length;
  // Away and back inside the app: the row is known archived now, so the pane opens no socket.
  await navigate(page, `/s/${API}/${API_ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await navigate(page, `/s/${PTY}/${PTY_ID}`);
  await expect(page.getByTestId("term-archived")).toBeVisible();
  await expect(strip(page)).toBeVisible();
  await page.waitForTimeout(1_500);
  expect(sockets.length).toBe(cold);
});
