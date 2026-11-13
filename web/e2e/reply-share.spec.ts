import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test, type Browser, type Page, type TestInfo } from "@playwright/test";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

// #1956: share ONE finished reply of an API session as a read-only link, in a real browser.
//
// The operator's side is the built SPA over mocked operator routes. The visitor is a FRESH
// context with no account: it is served the built `reply.html` at /r/<id> and the envelope at
// /api/public/reply/<id>, and every other /api answer is a loud 599, so a request the viewer
// should never make fails the test. The envelopes are the real ones the Python side seals
// (src/reply/fixtures/envelopes.json), opened with the real WebCrypto path.

const FX = JSON.parse(readFileSync(resolve(process.cwd(), "src/reply/fixtures/envelopes.json"), "utf8")) as {
  cases: { name: string; envelope: { id: string }; key: string; password?: string; content: { reply: string } }[];
};
const PLAIN = FX.cases.find((c) => !c.password)!;
const LOCKED = FX.cases.find((c) => c.password)!;
const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const KEY = `claude-api:${ID}`;
const TOOL_SUMMARY = "cat /home/u/proj/.env";
const PROMPT = "Rotate the indexing pools without downtime.";

function turn(over: Record<string, unknown>) {
  return {
    turn_id: "t1",
    operation_id: "",
    state: "completed",
    text: PROMPT,
    text_truncated: false,
    reply: PLAIN.content.reply,
    reply_ts: 1_800_000_000,
    reply_truncated: false,
    reason: null,
    tools: [
      { name: "shell", outcome: "ok", summary: TOOL_SUMMARY },
      { name: "read", outcome: "ok", summary: "src/pools.py" },
    ],
    tools_truncated: false,
    ts: 1_800_000_000,
    ...over,
  };
}

async function operatorPage(page: Page, made: { id: string; key: string; password: boolean }) {
  const created: unknown[] = [];
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "operator-csrf",
        new_session_engines: ["claude-api"],
        terminal_backend: "ws",
        auth_mode: "single-user",
        username: "marcus",
        avatar: null,
      },
    }),
  );
  const row = {
    id: KEY,
    engine: "claude-api",
    uuid: ID,
    short_uuid: ID.slice(0, 8),
    cwd: "/home/u/proj",
    project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
    last_mtime: Math.floor(Date.now() / 1000) - 60,
    first_user_message: PROMPT,
    title: "Pools",
    sticky: false,
    archived: false,
    ai_summary: "",
  };
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [row], next_offset: null, total: 1, facets: { projects: [], engines: [] } } }),
  );
  await page.route(new RegExp(`/api/sessions/claude-api(?::|%3A)${ID}$`), (r) => r.fulfill({ json: row }));
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(new RegExp(`/api/structured/sessions/claude-api(?::|%3A)${ID}(/[a-z]+)?(\\?.*)?$`), (r) => {
    if (/\/events(\?|$)/.test(r.request().url()))
      return r.fulfill({ json: { session_key: KEY, revision: 4, next_cursor: 4, events: [] } });
    return r.fulfill({
      json: {
        session_key: KEY,
        revision: 4,
        event_cursor: 4,
        cwd: "/home/u/proj",
        state: "running",
        active_turn: "t2",
        model_requested: null,
        model_effective: "claude-sonnet",
        turns: [turn({}), turn({ turn_id: "t2", state: "running", reply: "still going", tools: [] })],
        omitted_turns: 0,
        pending_requests: [],
        native: { native_id: "n1", worker: "w1", background_active: false },
        read_only: null,
      },
    });
  });
  await page.route("**/api/reply-shares", async (r) => {
    const req = r.request();
    if (req.method() !== "POST") return r.fallback();
    if (req.headers()["x-csrf-token"] !== "operator-csrf") return r.fulfill({ status: 403, json: { detail: "csrf" } });
    created.push(req.postDataJSON());
    return r.fulfill({
      status: 201,
      json: {
        share: {
          share_id: made.id,
          session_key: KEY,
          turn_id: "t1",
          agent: "Claude",
          has_password: made.password,
          expires_at: null,
          created_at: Date.now() / 1000,
          revoked_at: null,
          revoked_reason: null,
          open_count: 0,
          last_opened_at: null,
          live: true,
        },
        key: made.key,
        path: `/r/${made.id}`,
      },
    });
  });
  await mockRoster(page, {
    overrides: {
      "claude-api": {
        present: true,
        supports_new: true,
        api: { kind: "claude-agent-sdk", source: "claude", unavailable_reason: null, can_bypass: true },
      },
    },
  });
  return created;
}

async function visitor(browser: Browser, testInfo: TestInfo, envelope: unknown | null) {
  const use = testInfo.project.use;
  const ctx = await browser.newContext({
    viewport: use.viewport,
    userAgent: use.userAgent,
    deviceScaleFactor: use.deviceScaleFactor,
    isMobile: use.isMobile,
    hasTouch: use.hasTouch,
    baseURL: use.baseURL,
  });
  const requests: string[] = [];
  ctx.on("request", (req) => requests.push(req.url()));
  // The page the server answers at /r/<id> (`routes/reply_shares.reply_viewer`).
  await ctx.route(/\/r\/[A-Za-z0-9_-]*$/, async (r) => {
    const response = await r.fetch({ url: new URL("/reply.html", r.request().url()).toString() });
    await r.fulfill({ response });
  });
  await ctx.route("**/api/**", (r) => r.fulfill({ status: 599, json: { detail: "a route the viewer must not call" } }));
  await ctx.route(/\/api\/public\/reply\/[A-Za-z0-9_-]+$/, (r) =>
    envelope ? r.fulfill({ json: envelope }) : r.fulfill({ status: 404, json: { detail: "link unavailable" } }),
  );
  return { page: await ctx.newPage(), requests, ctx };
}

async function shareFromThePane(page: Page): Promise<void> {
  await page.goto(`/s/claude-api/${ID}`);
  const buttons = page.getByTestId("reply-share-button");
  await expect(buttons).toHaveCount(1); // the finished reply only; the running turn offers none
  await buttons.first().click();
  const dialog = page.getByTestId("reply-share");
  await expect(dialog).toBeVisible();
}

test("the operator shares a finished reply; a visitor reads only that reply", async ({ page, browser }, testInfo) => {
  const created = await operatorPage(page, { id: PLAIN.envelope.id, key: PLAIN.key, password: false });
  await shareFromThePane(page);
  if (testInfo.project.use.isMobile) {
    const box = (await page.getByTestId("reply-share-button").boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  await page.getByRole("button", { name: "1 day" }).click();
  await page.getByRole("button", { name: /Create link/ }).click();
  const url = await page.getByTestId("reply-share-url").inputValue();
  expect(created).toEqual([{ session: KEY, turn_id: "t1", expires_in: "1d" }]);
  // This page's own origin, the viewer path, and the key only in the fragment.
  const origin = new URL(page.url()).origin;
  expect(url).toBe(`${origin}/r/${PLAIN.envelope.id}#k=${PLAIN.key}`);

  const v = await visitor(browser, testInfo, PLAIN.envelope);
  await v.page.goto(url);
  const open = v.page.getByTestId("reply-open");
  await expect(open).toBeVisible({ timeout: 15_000 });
  await expect(open).toContainText("Split the pools");
  await expect(v.page.getByTestId("reply-tools-hidden")).toHaveText("4 commands & tools hidden");
  const text = await v.page.locator("body").innerText();
  for (const leak of [TOOL_SUMMARY, "src/pools.py", PROMPT, "/home/u/proj", "Pools"]) expect(text).not.toContain(leak);
  // One read, of ciphertext; the key went nowhere.
  const api = v.requests.filter((u) => u.includes("/api/"));
  expect(api).toEqual([`${origin}/api/public/reply/${PLAIN.envelope.id}`]);
  expect(v.requests.some((u) => u.includes(PLAIN.key))).toBe(false);
  // Nothing sideways on a phone.
  const overflow = await v.page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await v.ctx.close();
});

test("a password link asks first, and the password never leaves the page", async ({ page, browser }, testInfo) => {
  const created = await operatorPage(page, { id: LOCKED.envelope.id, key: LOCKED.key, password: true });
  await shareFromThePane(page);
  await page.getByLabel(/Password/).fill(LOCKED.password!);
  await page.getByRole("button", { name: /Create link/ }).click();
  await expect(page.getByTestId("reply-share-created")).toContainText("PASSWORD");
  expect((created[0] as { password?: string }).password).toBe(LOCKED.password);
  const url = await page.getByTestId("reply-share-url").inputValue();

  const v = await visitor(browser, testInfo, LOCKED.envelope);
  await v.page.goto(url);
  const field = v.page.getByLabel("Password");
  await expect(field).toBeVisible();
  await field.fill("not it");
  await v.page.getByRole("button", { name: "Open reply" }).click();
  await expect(v.page.getByRole("alert")).toHaveText("Wrong password.", { timeout: 20_000 });
  await field.fill(LOCKED.password!);
  await v.page.getByRole("button", { name: "Open reply" }).click();
  await expect(v.page.getByTestId("reply-open")).toBeVisible({ timeout: 20_000 });
  expect(v.requests.filter((u) => u.includes("/api/"))).toHaveLength(1);
  expect(v.requests.some((u) => u.includes(encodeURIComponent(LOCKED.password!)) || u.includes("not%20it"))).toBe(false);
  await v.ctx.close();
});

test("a revoked or expired link says so; a link without its key fetches nothing", async ({ browser }, testInfo) => {
  const gone = await visitor(browser, testInfo, null);
  await gone.page.goto(`/r/${PLAIN.envelope.id}#k=${PLAIN.key}`);
  await expect(gone.page.getByTestId("reply-unavailable")).toBeVisible();
  await gone.ctx.close();

  const cut = await visitor(browser, testInfo, PLAIN.envelope);
  await cut.page.goto(`/r/${PLAIN.envelope.id}`);
  await expect(cut.page.getByTestId("reply-incomplete")).toBeVisible();
  expect(cut.requests.filter((u) => u.includes("/api/"))).toEqual([]);
  await cut.ctx.close();
});

test("a message header's avatar, name, time and share icon sit on one centre line", async ({ page }) => {
  await operatorPage(page, { id: PLAIN.envelope.id, key: PLAIN.key, password: false });
  await page.goto(`/s/claude-api/${ID}`);
  await expect(page.getByTestId("reply-share-button")).toHaveCount(1);
  // Every header that carries a timestamp: the operator's and the agent's, the shared one too.
  const headers = page.locator("time").locator("xpath=..");
  await expect(headers.first()).toBeVisible();
  const spreads = await headers.evaluateAll((els) =>
    els.map((h) => {
      const mids = [...h.children].map((c) => {
        const r = c.getBoundingClientRect();
        return r.top + r.height / 2;
      });
      return { children: mids.length, spread: Math.max(...mids) - Math.min(...mids) };
    }),
  );
  expect(spreads.length).toBeGreaterThanOrEqual(2);
  expect(spreads.some((s) => s.children === 4)).toBe(true); // the shared reply: disc, name, time, icon
  for (const s of spreads) expect(s.spread).toBeLessThanOrEqual(1);
});
