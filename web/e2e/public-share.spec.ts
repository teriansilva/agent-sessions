import {
  expect,
  test,
  type Browser,
  type BrowserContext,
  type Locator,
  type Page,
  type Route,
  type TestInfo,
} from "@playwright/test";
import { mockRoster } from "./roster";
import { settingsPath } from "../src/routes/settingsTabs";

// #1404 P4a: public share links for API sessions, end to end in a real browser.
//
// The operator makes a link in **Share publicly…**; a visitor opens it in a FRESH browser context
// (no operator state at all), unlocks it, watches, sends (send links), and loses access the moment
// the operator revokes. The server is a small in-test model of `routes/shares.py` +
// `routes/public_api.py`, shared by both contexts, so a revoke in one is the visitor's next 404.
// `/p/<token>` and `/p/` serve the built `share.html`, as the server does.

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const KEY = `codex-api:${ID}`;
const URL_PATH = `/s/codex-api/${ID}`;
const TITLE = "Fix the auth redirect loop";
const PASSWORD = "correct horse";
const VISITOR_CSRF = "visitor-csrf-token";
const SEND_RISK =
  "Anyone with this link can instruct the agent with this session's permissions, including reaching saved secret values the agent can see.";
const API_DISCLOSURE =
  "Visitors see the conversation and its attached images. Only saved secret values are hidden; anything else typed or printed in it is visible.";
const SHOWN_ONCE = "Only a fingerprint of the link is stored. Copy it now; to share again, create a new link.";

type Json = Record<string, unknown>;

interface Row {
  share: Json & { share_id: string; mode: string; revoked_at: number | null };
  token: string;
  password: string | null;
}

/** The server, as far as these flows see it. */
class FakeServer {
  rows: Row[] = [];
  created: Json[] = [];
  /** The visitor's share cookie, modelled: which link it names and the name it carries. */
  cookie: { shareId: string; name: string | null } | null = null;
  turnBodies: { body: Json; csrf: string | undefined }[] = [];
  revision = 3;
  running = false;
  /** A background task on the worker, no turn running (#1430). */
  background = false;
  turns: Json[] = [
    {
      turn_id: "t1",
      state: "completed",
      delivery: null,
      text: "Login bounces between /login and / after the cookie refresh. Find out why.",
      text_truncated: false,
      reply: "The refresh middleware re-issues the cookie with `Path=/api`, so the SPA shell never sees it.",
      reply_ts: 1_800_000_000,
      reply_truncated: false,
      attachments: [],
      tools: [{ name: "read", outcome: "ok", summary: "src/auth/middleware.py · 212 lines" }],
      tools_truncated: false,
      author: null,
      mine: false,
    },
  ];

  live(): Row[] {
    return this.rows.filter((r) => r.share.revoked_at == null);
  }
  byToken(token: string): Row | undefined {
    return this.live().find((r) => r.token === token);
  }
  visitorRow(): Row | undefined {
    return this.cookie ? this.live().find((r) => r.share.share_id === this.cookie!.shareId) : undefined;
  }
  /** Per-link turns beyond the first link's (`turns`), keyed by share id. */
  extraTurns: Record<string, Json[]> = {};
  link(row: Row) {
    return `L-${row.share.share_id}`;
  }
  view(row: Row) {
    const name = this.cookie?.name ?? null;
    return {
      link: this.link(row),
      kind: "api",
      mode: row.share.mode,
      title: row.share.title,
      expires_at: row.share.expires_at ?? null,
      name,
      name_required: row.share.mode === "send" && !name,
    };
  }
  snapshot() {
    const row = this.visitorRow()!;
    const turns = this.extraTurns[row.share.share_id] ?? this.turns;
    return {
      link: this.link(row),
      revision: this.revision,
      event_cursor: this.revision,
      state: this.running ? "running" : "idle",
      active_turn: this.running ? "t1" : null,
      model: "gpt-5-codex",
      turns,
      omitted_turns: 0,
      pending_requests: this.running
        ? [{ request_id: "r1", turn_id: "t1", kind: "command", summary: "Codex wants to edit src/auth/middleware.py." }]
        : [],
      read_only: false,
      images: true,
      can_send: this.visitorRow()?.share.mode === "send",
      mission_held: false,
      background: this.background,
    };
  }
}

async function operatorSetup(page: Page, server: FakeServer, theme: "dark" | "light" = "dark") {
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "operator-csrf",
        new_session_engines: ["codex-api"],
        terminal_backend: "ws",
        auth_mode: "single-user",
        default_project: "/home/u/proj",
        overview_expanded: [],
        projects_hidden: [],
        theme,
      },
    }),
  );
  const row = {
    id: KEY,
    engine: "codex-api",
    uuid: ID,
    short_uuid: ID.slice(0, 8),
    cwd: "/home/u/proj",
    project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
    last_mtime: Math.floor(Date.now() / 1000) - 60,
    first_user_message: TITLE,
    title: TITLE,
    sticky: false,
    archived: false,
    ai_summary: "",
  };
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [row], next_offset: null, total: 1, facets: { projects: [], engines: [] } } }),
  );
  await page.route(new RegExp(`/api/sessions/codex-api(?::|%3A)${ID}$`), (r) => r.fulfill({ json: row }));
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}(/[a-z]+)?(\\?.*)?$`), (r) => {
    const sub = r.request().url().match(/\/(events)(\?|$)/)?.[1];
    const snap = {
      session_key: KEY,
      revision: 4,
      event_cursor: 4,
      cwd: "/home/u/proj",
      state: "idle",
      active_turn: null,
      model_requested: null,
      model_effective: "gpt-5-codex",
      turns: [],
      omitted_turns: 0,
      pending_requests: [],
      native: { native_id: "n1", worker: null, background_active: false },
      read_only: null,
    };
    if (sub === "events") return r.fulfill({ json: { session_key: KEY, revision: 4, next_cursor: 4, events: [] } });
    return r.fulfill({ json: snap });
  });
  await mockRoster(page, {
    overrides: {
      "codex-api": {
        present: true,
        supports_new: true,
        api: { kind: "codex-app-server", source: "codex", unavailable_reason: null, can_bypass: true },
      },
    },
  });
  // The operator's share routes (`routes/shares.py`).
  await page.route(/\/api\/shares(\/[^?]*)?(\?.*)?$/, async (r: Route) => {
    const req = r.request();
    const url = new URL(req.url());
    if (req.method() !== "GET" && req.headers()["x-csrf-token"] !== "operator-csrf") {
      return r.fulfill({ status: 403, json: { detail: "bad csrf" } });
    }
    if (req.method() === "GET" && url.pathname === "/api/shares") {
      const session = url.searchParams.get("session");
      const shares = server
        .live()
        .filter((x) => !session || x.share.session_key === session)
        .map((x) => x.share);
      return r.fulfill({ json: { available: true, reason: null, shares } });
    }
    if (req.method() === "POST" && url.pathname === "/api/shares") {
      const body = req.postDataJSON() as Json;
      server.created.push(body);
      if (body.mode === "send" && body.accept_risk !== true) {
        return r.fulfill({ status: 422, json: { detail: `${SEND_RISK} Send accept_risk: true to create it.` } });
      }
      const n = server.rows.length + 1;
      const token = `tok${n}`.padEnd(43, "x");
      const share = {
        share_id: `share${n}`,
        session_key: body.session,
        kind: "api",
        mode: body.mode,
        title: body.title,
        has_password: !!body.password,
        expires_at: body.expires_at ?? null,
        created_at: Date.now() / 1000,
        revoked_at: null,
        revoked_reason: null,
        open_count: 0,
        last_opened_at: null,
      };
      server.rows.push({ share, token, password: (body.password as string) || null });
      const made: Json = { share, url: `${url.origin}/p/${token}` };
      if (body.mode === "send") made.risk = SEND_RISK;
      return r.fulfill({ status: 201, json: made });
    }
    if (req.method() === "POST" && url.pathname === "/api/shares/revoke-all") {
      const live = server.live();
      live.forEach((x) => (x.share.revoked_at = Date.now() / 1000));
      return r.fulfill({ json: { revoked: live.length } });
    }
    const m = /^\/api\/shares\/([^/]+)$/.exec(url.pathname);
    if (req.method() === "DELETE" && m) {
      const row = server.rows.find((x) => x.share.share_id === m[1]);
      if (!row) return r.fulfill({ status: 404, json: { detail: "unknown link" } });
      row.share.revoked_at = Date.now() / 1000;
      return r.fulfill({ json: { revoked: true } });
    }
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
}

/** A visitor: a FRESH context with the project's device, nothing of the operator's. */
async function visitorContext(browser: Browser, testInfo: TestInfo, server: FakeServer): Promise<BrowserContext> {
  const use = testInfo.project.use;
  const ctx = await browser.newContext({
    viewport: use.viewport,
    userAgent: use.userAgent,
    deviceScaleFactor: use.deviceScaleFactor,
    isMobile: use.isMobile,
    hasTouch: use.hasTouch,
    baseURL: use.baseURL,
  });
  // The page the server answers at /p/<token> and /p/ (`routes/shares.share_viewer`).
  await ctx.route(/\/p\/[A-Za-z0-9_-]*$/, async (r) => {
    const response = await r.fetch({ url: new URL("/share.html", r.request().url()).toString() });
    await r.fulfill({ response });
  });
  const gone = (r: Route) => r.fulfill({ status: 404, json: { detail: "link unavailable" } });
  await ctx.route("**/api/**", (r) => r.fulfill({ status: 599, json: { detail: "operator route from the viewer" } }));
  await ctx.route(/\/api\/public\/.*/, async (r) => {
    const req = r.request();
    const url = new URL(req.url());
    const path = url.pathname;
    const csrfOk = req.headers()["x-csrf-token"] === VISITOR_CSRF;
    if (path === "/api/public/unlock") {
      const body = req.postDataJSON() as { token?: string; password?: string };
      const row = server.byToken(body.token ?? "");
      if (!row) return gone(r);
      if (row.password) {
        if (!body.password) return r.fulfill({ status: 401, json: { detail: "password required", password_required: true } });
        if (body.password !== row.password) {
          return r.fulfill({ status: 401, json: { detail: "wrong password", password_required: true, tries_left: 4 } });
        }
      }
      row.share.open_count = (row.share.open_count as number) + 1;
      row.share.last_opened_at = Date.now() / 1000;
      server.cookie = { shareId: row.share.share_id, name: null };
      return r.fulfill({ json: { share: server.view(row), csrf: VISITOR_CSRF } });
    }
    if (!server.cookie) return r.fulfill({ status: 401, json: { detail: "unlock this link first" } });
    const row = server.visitorRow();
    if (!row) return gone(r);
    if (path === "/api/public/share") return r.fulfill({ json: { share: server.view(row), csrf: VISITOR_CSRF } });
    if (path === "/api/public/session") return r.fulfill({ json: server.snapshot() });
    if (path === "/api/public/events") {
      return r.fulfill({
        json: { link: server.link(row), revision: server.revision, next_cursor: server.revision, events: [] },
      });
    }
    if (path === "/api/public/name") {
      if (!csrfOk) return r.fulfill({ status: 403, json: { detail: "bad csrf" } });
      server.cookie.name = (req.postDataJSON() as { name: string }).name;
      return r.fulfill({ json: { share: server.view(row) } });
    }
    if (path === "/api/public/turns") {
      if (row.share.mode !== "send") return r.fulfill({ status: 403, json: { detail: "this link is view only" } });
      if (!csrfOk) return r.fulfill({ status: 403, json: { detail: "bad csrf" } });
      if (!server.cookie.name) return r.fulfill({ status: 403, json: { detail: "enter your name first" } });
      const body = req.postDataJSON() as Json;
      server.turnBodies.push({ body, csrf: req.headers()["x-csrf-token"] });
      server.revision += 1;
      server.turns = [
        ...server.turns,
        {
          turn_id: `v${server.revision}`,
          state: "completed",
          delivery: null,
          text: body.text,
          text_truncated: false,
          reply: "Checked: logout clears the cookie on the same path.",
          reply_ts: 1_800_000_100,
          reply_truncated: false,
          attachments: [],
          tools: [],
          tools_truncated: false,
          author: { name: server.cookie.name, via: "public_link" },
          mine: true,
        },
      ];
      return r.fulfill({ status: 200, json: { state: "completed" } });
    }
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
  return ctx;
}

/** Run a head action wherever the header put it: inline, the "…" fold, or the ≤800px menu. */
async function headAction(page: Page, aria: string) {
  const pane = page.getByTestId("structured-pane");
  const trigger = pane.getByTestId("head-actions-menu");
  const chip = pane.locator("[data-head-action]").first();
  await expect(trigger.or(chip).first()).toBeVisible();
  let attempt = 0;
  await expect(async () => {
    if (attempt++ > 0) await page.keyboard.press("Escape");
    if (await trigger.isVisible()) {
      await trigger.click({ timeout: 2000 });
    } else {
      const inline = pane.locator("[data-head-action]").and(pane.getByRole("button", { name: aria }));
      if (await inline.isVisible()) return inline.click({ timeout: 2000 });
      await pane.getByRole("button", { name: "More session actions" }).click({ timeout: 2000 });
    }
    await page.getByRole("menuitem", { name: aria }).click({ timeout: 2000 });
  }).toPass({ timeout: 15_000 });
}

/** On screen AND inside the dialog's own box with the dialog unscrolled: readable as it opens. */
async function visibleWithoutScrolling(el: Locator) {
  await expect(el).toBeVisible();
  const geo = await el.evaluate((node) => {
    const dialog = node.closest('[role="dialog"]') as HTMLElement;
    const d = dialog.getBoundingClientRect();
    const e = node.getBoundingClientRect();
    return {
      scrollTop: dialog.scrollTop,
      insideDialog: e.top >= d.top - 0.5 && e.bottom <= d.bottom + 0.5,
      onScreen: e.top >= 0 && e.bottom <= window.innerHeight,
    };
  });
  expect(geo).toEqual({ scrollTop: 0, insideDialog: true, onScreen: true });
}

async function controlsAtLeast44(scope: Locator) {
  const small = await scope.evaluate((root) =>
    [...root.querySelectorAll<HTMLElement>("button, input, textarea, a[href]")]
      .filter((el) => el.offsetParent !== null && (el as HTMLInputElement).type !== "file")
      .map((el) => ({ el: el.getAttribute("aria-label") || el.textContent?.trim() || el.tagName, r: el.getBoundingClientRect() }))
      // An inline text link in running copy is exempt from the 44px floor (WCAG 2.5.8 inline).
      .filter(({ r }) => r.height < 44)
      .map(({ el, r }) => `${el}: ${Math.round(r.height)}px`),
  );
  expect(small.filter((s) => !s.startsWith("Settings → Sharing"))).toEqual([]);
}

async function noHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

async function openDialog(page: Page) {
  await page.goto(URL_PATH);
  await expect(page.getByTestId("structured-empty")).toBeVisible();
  await headAction(page, "Share this session publicly");
  const dialog = page.getByRole("dialog", { name: "Share publicly" });
  await expect(dialog).toBeVisible();
  return dialog;
}

test("view link with a password: create, open in a fresh context, unlock, watch; the token leaves the address bar (#1404)", async ({
  page,
  browser,
}, testInfo) => {
  const server = new FakeServer();
  await operatorSetup(page, server, "dark");
  const dialog = await openDialog(page);

  // The disclosure is on screen as the dialog opens; view is the default access.
  await visibleWithoutScrolling(dialog.getByTestId("share-disclosure"));
  await expect(dialog.getByTestId("share-disclosure")).toHaveText(API_DISCLOSURE);
  await expect(dialog.getByRole("radio", { name: /View only/ })).toHaveAttribute("aria-checked", "true");
  await expect(dialog.getByTestId("share-risk")).toHaveCount(0);
  await controlsAtLeast44(dialog);

  await dialog.getByLabel(/^Password/).fill(PASSWORD);
  await dialog.getByRole("button", { name: "7 days" }).click();
  await dialog.getByRole("button", { name: /Create public link/ }).click();

  const url = dialog.getByTestId("share-url");
  await expect(url).toHaveValue(/\/p\/tok1x+$/);
  await visibleWithoutScrolling(url);
  await visibleWithoutScrolling(dialog.getByTestId("share-once"));
  await expect(dialog.getByTestId("share-once")).toHaveText(SHOWN_ONCE);
  await visibleWithoutScrolling(dialog.getByTestId("share-disclosure"));
  expect(server.created).toHaveLength(1);
  expect(server.created[0]).toMatchObject({ session: KEY, mode: "view", password: PASSWORD, title: TITLE });
  expect(server.created[0]).not.toHaveProperty("accept_risk");
  expect(server.created[0].expires_at as number).toBeGreaterThan(Date.now() / 1000 + 6.9 * 86400);
  // The new link is listed (never with its URL), flagged with what it allows.
  const listed = dialog.locator("[data-share-row]");
  await expect(listed).toHaveCount(1);
  await expect(listed).toContainText("VIEW ONLY");
  await expect(listed).toContainText("PASSWORD");
  await expect(listed).not.toContainText("tok1");
  await controlsAtLeast44(dialog);
  const link = await url.inputValue();
  const token = new URL(link).pathname.split("/")[2];

  // A fresh browser context: no operator cookie, storage or CSRF.
  const ctx = await visitorContext(browser, testInfo, server);
  const visitor = await ctx.newPage();
  const apiCalls: string[] = [];
  visitor.on("request", (r) => {
    const p = new URL(r.url()).pathname;
    if (p.startsWith("/api/")) apiCalls.push(p);
  });
  await visitor.goto(link);
  const gate = visitor.getByTestId("share-password-gate");
  await expect(gate).toBeVisible();
  await expect(gate).toContainText("Someone shared a BattleLab session with you");
  await controlsAtLeast44(gate);
  await gate.getByLabel("Password").fill("wrong");
  await gate.getByRole("button", { name: "Open session" }).click();
  await expect(gate.getByRole("alert")).toHaveText("Wrong password. 4 tries left before a 15-minute pause.");
  await gate.getByLabel("Password").fill(PASSWORD);
  await gate.getByRole("button", { name: "Open session" }).click();

  const transcript = visitor.getByTestId("share-transcript");
  await expect(transcript).toContainText("Login bounces between /login and / after the cookie refresh.");
  await expect(transcript).toContainText("Operator");
  // The agent's commands & tools are folded behind one line, as in the operator's pane: the reply
  // reads without scrolling past them, and a tap opens them.
  const activity = transcript.getByTestId("share-activity");
  await expect(activity.locator("summary")).toHaveText("1 command & tool");
  const step = activity.getByText("src/auth/middleware.py · 212 lines");
  await expect(step).toBeHidden();
  await activity.locator("summary").click();
  await expect(step).toBeVisible();
  await expect(visitor.getByTestId("share-view-only")).toBeVisible();
  await expect(visitor.getByTestId("share-composer")).toHaveCount(0);
  await expect(visitor.getByText(TITLE)).toBeVisible();
  await expect(visitor.getByRole("banner").getByText("VIEW ONLY", { exact: true })).toBeVisible();

  // The token is out of the address bar (and the current history entry) once it has been traded.
  // The viewer offers no app install: share.html carries no manifest link.
  await expect(visitor.locator('link[rel="manifest"]')).toHaveCount(0);
  await expect(visitor).toHaveURL(/\/p\/$/);
  expect(visitor.url()).not.toContain(token);
  // A reload there re-reads the link through the cookie, without the token.
  await visitor.reload();
  await expect(transcript).toContainText("Login bounces between /login and / after the cookie refresh.");
  expect(visitor.url()).not.toContain(token);
  // The viewer never calls an operator route.
  expect(apiCalls.length).toBeGreaterThan(0);
  expect(apiCalls.filter((p) => !p.startsWith("/api/public/"))).toEqual([]);

  // Nothing scrolls sideways at 390px, on either side.
  await visitor.setViewportSize({ width: 390, height: 844 });
  await noHorizontalOverflow(visitor);
  await page.setViewportSize({ width: 390, height: 844 });
  await noHorizontalOverflow(page);
  await ctx.close();
});

test("send link: the RISKY warning before creating, accept_risk sent; the visitor names themselves, then sends (#1404)", async ({
  page,
  browser,
}, testInfo) => {
  const server = new FakeServer();
  await operatorSetup(page, server, "light");
  const dialog = await openDialog(page);
  await dialog.getByRole("radio", { name: /Can send messages/ }).click();
  const risk = dialog.getByTestId("share-risk");
  await visibleWithoutScrolling(risk);
  await expect(risk).toContainText("RISKY");
  await expect(risk).toContainText(SEND_RISK);
  await expect(risk).toContainText("saved secret values the agent can see");
  await visibleWithoutScrolling(dialog.getByTestId("share-disclosure"));
  // The title visitors see starts as the session's and can be changed.
  const title = dialog.getByLabel("Title visitors see");
  await expect(title).toHaveValue(TITLE);
  await title.fill("Auth loop — shared");
  await dialog.getByRole("button", { name: "Never" }).click();
  await dialog.getByRole("button", { name: /Create public link/ }).click();
  await expect(dialog.getByTestId("share-url")).toHaveValue(/\/p\/tok1x+$/);
  expect(server.created[0]).toMatchObject({
    mode: "send",
    accept_risk: true,
    expires_at: null,
    title: "Auth loop — shared",
  });
  expect(server.created[0]).not.toHaveProperty("password");
  // After creating, the risk and the shown-once copy are still on screen with the link.
  await visibleWithoutScrolling(dialog.getByTestId("share-risk"));
  await visibleWithoutScrolling(dialog.getByTestId("share-once"));
  await visibleWithoutScrolling(dialog.getByTestId("share-url"));
  const row = dialog.locator("[data-share-row]");
  await expect(row).toContainText("CAN SEND MESSAGES");
  await expect(row).toContainText("NO PASSWORD");
  await expect(row).toContainText("NEVER EXPIRES");
  const link = await dialog.getByTestId("share-url").inputValue();

  const ctx = await visitorContext(browser, testInfo, server);
  const visitor = await ctx.newPage();
  await visitor.goto(link);
  // No password on this link: straight in. Watching needs no name; sending does.
  await expect(visitor.getByTestId("share-transcript")).toContainText("Find out why.");
  await expect(visitor).toHaveURL(/\/p\/$/);
  const nameForm = visitor.getByTestId("share-name-form");
  await expect(nameForm).toContainText("Anyone can type any name, so it isn’t a verified identity.");
  await expect(visitor.getByTestId("share-composer")).toHaveCount(0);
  await nameForm.getByLabel("Your name — shown with your messages").fill("Alex");
  await nameForm.getByRole("button", { name: "Continue" }).click();

  const composer = visitor.getByTestId("share-composer");
  await expect(composer).toContainText("Sending as Alex");
  await controlsAtLeast44(composer);
  await composer.getByLabel("Message the agent").fill("Can you also check the logout path?");
  await composer.getByRole("button", { name: "Send" }).click();
  await expect(visitor.getByTestId("share-transcript")).toContainText("Alex · via public link");
  await expect(visitor.getByTestId("share-transcript")).toContainText("Can you also check the logout path?");
  expect(server.turnBodies).toHaveLength(1);
  expect(server.turnBodies[0].csrf).toBe(VISITOR_CSRF);
  expect(server.turnBodies[0].body).toMatchObject({ text: "Can you also check the logout path?", attachments: [] });
  expect(server.turnBodies[0].body.client_key).toMatch(/^[0-9a-f-]{36}$/);
  await expect(composer.getByLabel("Message the agent")).toHaveValue("");
  await visitor.setViewportSize({ width: 390, height: 844 });
  await noHorizontalOverflow(visitor);
  await ctx.close();
});

test("revoke: the visitor's next request finds the link gone; Settings → Sharing lists and revokes all (#1404)", async ({
  page,
  browser,
}, testInfo) => {
  const server = new FakeServer();
  server.running = true; // a working agent: the viewer polls every second
  await operatorSetup(page, server, "dark");
  const dialog = await openDialog(page);
  await dialog.getByRole("button", { name: /Create public link/ }).click();
  const link = await dialog.getByTestId("share-url").inputValue();
  await dialog.getByRole("button", { name: "Close" }).click();

  const ctx = await visitorContext(browser, testInfo, server);
  const visitor = await ctx.newPage();
  await visitor.goto(link);
  await expect(visitor.getByTestId("share-approval")).toContainText("Waiting for the owner. Only the owner can approve.");
  await expect(visitor.getByText("WORKING", { exact: true })).toBeVisible();

  // Settings → Sharing: every live link, with Revoke and Revoke all.
  await page.goto(settingsPath("sharing"));
  const panel = page.getByTestId("sharing-settings");
  await expect(panel.getByRole("heading", { name: "Public links · 1 active" })).toBeVisible();
  await expect(panel.locator("[data-share-row]")).toContainText(TITLE);
  await expect(panel.locator("[data-share-row]")).toContainText("opened 1×");
  await controlsAtLeast44(panel);
  await panel.getByRole("button", { name: /^Revoke view only link/ }).click();
  await expect(panel.getByRole("heading", { name: "Public links · 0 active" })).toBeVisible();

  // The visitor's next poll is a 404: the viewer says so, and keeps nothing on screen.
  await expect(visitor.getByTestId("share-unavailable")).toBeVisible();
  await expect(visitor.getByTestId("share-unavailable")).toContainText("This link no longer works");
  await expect(visitor.getByTestId("share-transcript")).toHaveCount(0);
  // Opening the link again is the same 404.
  await visitor.goto(link);
  await expect(visitor.getByTestId("share-unavailable")).toBeVisible();

  // Revoke all, behind one confirmation.
  server.rows.push(
    ...[1, 2].map((i) => ({
      token: `more${i}`.padEnd(43, "y"),
      password: null,
      share: {
        share_id: `more${i}`,
        session_key: KEY,
        kind: "api",
        mode: "view",
        title: TITLE,
        has_password: false,
        expires_at: null,
        created_at: Date.now() / 1000,
        revoked_at: null,
        revoked_reason: null,
        open_count: 0,
        last_opened_at: null,
      },
    })),
  );
  await page.reload();
  await expect(panel.getByRole("heading", { name: "Public links · 2 active" })).toBeVisible();
  await panel.getByRole("button", { name: "Revoke all", exact: true }).click();
  await panel.getByRole("button", { name: "Revoke all links" }).click();
  await expect(panel.getByRole("heading", { name: "Public links · 0 active" })).toBeVisible();
  expect(server.live()).toHaveLength(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await noHorizontalOverflow(page);
  await ctx.close();
});

test("two links in one browser: a page never shows the other link's conversation under its title (#1404, Hermes on #1444)", async ({
  page,
  browser,
}, testInfo) => {
  const server = new FakeServer();
  server.running = true; // the viewer polls every second
  await operatorSetup(page, server, "dark");
  const links: string[] = [];
  for (const title of ["Link A", "Link B"]) {
    const dialog = await openDialog(page);
    await dialog.getByLabel("Title visitors see").fill(title);
    await dialog.getByRole("button", { name: /Create public link/ }).click();
    links.push(await dialog.getByTestId("share-url").inputValue());
    await dialog.getByRole("button", { name: "Close" }).click();
  }
  server.extraTurns.share2 = [
    { ...server.turns[0], turn_id: "b1", text: "B-ONLY conversation", reply: "B-ONLY reply" },
  ];

  const ctx = await visitorContext(browser, testInfo, server);
  const first = await ctx.newPage();
  await first.goto(links[0]);
  await expect(first.getByRole("banner")).toContainText("Link A");
  await expect(first.getByTestId("share-transcript")).toContainText("Find out why.");
  // Record, in the page itself, any moment it shows one link's conversation under the other's title.
  await first.evaluate(() => {
    const w = window as unknown as { __mixed: boolean };
    w.__mixed = false;
    const check = () => {
      const title = document.querySelector("header")?.textContent ?? "";
      const body = document.querySelector('[data-testid="share-transcript"]')?.textContent ?? "";
      // Either direction: B's conversation under A's title, or A's under B's.
      if (title.includes("Link A") && body.includes("B-ONLY")) w.__mixed = true;
      if (title.includes("Link B") && body.includes("Find out why.")) w.__mixed = true;
    };
    new MutationObserver(check).observe(document.body, { subtree: true, childList: true, characterData: true });
  });

  // A second tab in the SAME browser opens link B: the one share cookie now names B.
  const second = await ctx.newPage();
  await second.goto(links[1]);
  await expect(second.getByRole("banner")).toContainText("Link B");
  await expect(second.getByTestId("share-transcript")).toContainText("B-ONLY conversation");

  // The first tab's next poll answers for B: it follows the cookie to B, as one consistent view.
  await expect(first.getByRole("banner")).toContainText("Link B");
  await expect(first.getByTestId("share-transcript")).toContainText("B-ONLY conversation");
  await expect(first.getByTestId("share-transcript")).not.toContainText("Find out why.");
  expect(await first.evaluate(() => (window as unknown as { __mixed: boolean }).__mixed)).toBe(false);
  await ctx.close();
});

test("the agent at work: the working animation with its latest step, and a background task (#1404)", async ({
  page,
  browser,
}, testInfo) => {
  const server = new FakeServer();
  server.running = true;
  server.turns = [
    {
      ...server.turns[0],
      state: "running",
      reply: null,
      tools: [
        { name: "read", outcome: "ok", summary: "src/auth/middleware.py · 212 lines" },
        { name: "bash", outcome: null, summary: "grep -rn Path=/api src/auth" },
      ],
    },
  ];
  await operatorSetup(page, server, "dark");
  const dialog = await openDialog(page);
  await dialog.getByRole("button", { name: /Create public link/ }).click();
  const link = await dialog.getByTestId("share-url").inputValue();

  const ctx = await visitorContext(browser, testInfo, server);
  const visitor = await ctx.newPage();
  await visitor.goto(link);
  const transcript = visitor.getByTestId("share-transcript");
  // The steps stay folded; the working row animates and names the step running now.
  const working = transcript.getByTestId("share-working");
  await expect(working).toBeVisible();
  await expect(working).toHaveText("Working · bash grep -rn Path=/api src/auth");
  const bar = working.locator('[aria-hidden="true"] > span').first();
  await expect(bar).toBeVisible();
  expect(await bar.evaluate((el) => getComputedStyle(el).animationName)).toMatch(/pulse/);
  await expect(transcript.getByTestId("share-activity").locator("summary")).toHaveText("2 commands & tools · working");
  await expect(transcript.getByText("src/auth/middleware.py · 212 lines")).toBeHidden();
  await expect(transcript.getByTestId("share-background-work")).toHaveCount(0);
  await noHorizontalOverflow(visitor);

  // The turn ends; a background task keeps the agent busy: its own row, and the status says so.
  server.running = false;
  server.background = true;
  server.turns = [{ ...server.turns[0], state: "completed", reply: "Found it." }];
  server.revision += 1;
  const background = transcript.getByTestId("share-background-work");
  await expect(background).toBeVisible({ timeout: 15_000 });
  await expect(background).toHaveText("A background task is running");
  await expect(transcript.getByTestId("share-working")).toHaveCount(0);
  await expect(visitor.getByText("WORKING", { exact: true })).toBeVisible();

  // It finishes: the row goes, the status is idle again.
  server.background = false;
  server.revision += 1;
  await expect(background).toHaveCount(0, { timeout: 15_000 });
  await expect(visitor.getByText("IDLE", { exact: true })).toBeVisible();
  await ctx.close();
});
