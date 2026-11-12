import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test, type Browser, type Locator, type Page, type TestInfo } from "@playwright/test";
import { mockRoster } from "./roster";
import { settingsPath } from "../src/routes/settingsTabs";
import { TOUCH_FLOOR } from "./touchTarget";

// #1931: avatars beside every message and the "N connected" count, in a real browser.
//
// Three surfaces: the public share viewer (a fresh visitor context on the built `share.html`), the
// operator's own API-session pane, and Settings › Profile, where an image is chosen, cropped and
// scaled by the browser's own canvas, and uploaded. A jsdom test proves none of the geometry this
// is about — that the disc is round, sits beside its label, and doesn't push the text off a phone.

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const KEY = `codex-api:${ID}`;
const TOKEN = "avatars".padEnd(43, "x");
const ICON = readFileSync(resolve(process.cwd(), "public/apple-touch-icon.png")); // 180×180 PNG
const WIDE = resolve(process.cwd(), "src/assets/hero.png"); // 343×361: not square

const OPERATOR_TURN = {
  turn_id: "t1",
  state: "completed",
  delivery: null,
  text: "Dedicate one qwen replica to indexing and keep the other for chat.",
  text_truncated: false,
  reply: "Feasible: split the pools, one 128k replica for indexing.",
  reply_ts: 1_800_000_000,
  reply_truncated: false,
  attachments: [],
  tools: [],
  tools_truncated: false,
  author: null,
  mine: false,
};
const VISITOR_TURN = {
  ...OPERATOR_TURN,
  turn_id: "t2",
  text: "Does the embedding volume move with it?",
  reply: null,
  reply_ts: null,
  author: { name: "Jana", via: "public_link" as const },
};

/** The disc must be round, the size it was given, and left of its label without overlapping it. */
async function expectDiscBeside(disc: Locator, label: Locator, size: number) {
  await expect(disc).toBeVisible();
  const [d, l] = [await disc.boundingBox(), await label.boundingBox()];
  expect(d && l).toBeTruthy();
  expect(Math.abs(d!.width - size)).toBeLessThanOrEqual(1);
  expect(Math.abs(d!.height - d!.width)).toBeLessThanOrEqual(1);
  expect(d!.x + d!.width).toBeLessThanOrEqual(l!.x + 0.5);
  expect(await disc.evaluate((e) => getComputedStyle(e).borderTopLeftRadius)).toBe("50%");
}

/** The `file` part of a multipart body: its declared type and its bytes. */
function filePart(body: Buffer, contentType: string): { type: string; data: Buffer } | null {
  const boundary = /boundary=("?)([^";]+)\1/.exec(contentType)?.[2];
  if (!boundary) return null;
  const delim = Buffer.from(`--${boundary}`);
  let at = body.indexOf(delim);
  while (at !== -1) {
    const next = body.indexOf(delim, at + delim.length);
    if (next === -1) break;
    const part = body.subarray(at + delim.length + 2, next - 2); // skip CRLF after delim / before next
    const split = part.indexOf("\r\n\r\n");
    const head = part.subarray(0, split).toString("utf8");
    if (/name="file"/.test(head)) {
      return { type: /content-type:\s*([^\r\n]+)/i.exec(head)?.[1] ?? "", data: part.subarray(split + 4) };
    }
    at = next;
  }
  return null;
}

async function visitor(browser: Browser, testInfo: TestInfo): Promise<Page> {
  const use = testInfo.project.use;
  const ctx = await browser.newContext({
    viewport: use.viewport,
    userAgent: use.userAgent,
    deviceScaleFactor: use.deviceScaleFactor,
    isMobile: use.isMobile,
    hasTouch: use.hasTouch,
    baseURL: use.baseURL,
  });
  await ctx.route(/\/p\/[A-Za-z0-9_-]*$/, async (r) => {
    const response = await r.fetch({ url: new URL("/share.html", r.request().url()).toString() });
    await r.fulfill({ response });
  });
  await ctx.route("**/api/**", (r) => r.fulfill({ status: 599, json: { detail: "operator route from the viewer" } }));
  const share = {
    link: "L-1",
    kind: "api",
    mode: "view",
    title: "Investigate AI performance",
    expires_at: null,
    name: null,
    name_required: false,
  };
  await ctx.route(/\/api\/public\/.*/, async (r) => {
    const path = new URL(r.request().url()).pathname;
    if (path === "/api/public/unlock" || path === "/api/public/share")
      return r.fulfill({ json: { share, csrf: "c" } });
    if (path === "/api/public/avatar") return r.fulfill({ body: ICON, contentType: "image/png" });
    if (path === "/api/public/events")
      return r.fulfill({ json: { link: "L-1", revision: 3, next_cursor: 3, events: [] } });
    if (path === "/api/public/session")
      return r.fulfill({
        json: {
          link: "L-1",
          revision: 3,
          event_cursor: 3,
          state: "idle",
          active_turn: null,
          model: null,
          turns: [OPERATOR_TURN, VISITOR_TURN],
          omitted_turns: 0,
          pending_requests: [],
          read_only: false,
          images: true,
          can_send: false,
          mission_held: false,
          operator_avatar: "v1",
          present: 3,
        },
      });
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
  return ctx.newPage();
}

test("the share viewer: discs beside each author, and the top bar counts who is here", async ({
  browser,
}, testInfo) => {
  const page = await visitor(browser, testInfo);
  await page.goto(`/p/${TOKEN}`);
  await expect(page.getByTestId("share-present")).toHaveText("3 CONNECTED");
  await expect(page.getByTestId("share-present")).toBeInViewport();

  const operator = page.locator('[data-author="operator"]');
  const op = operator.locator("[data-avatar]");
  await expectDiscBeside(op, operator.getByText("Operator", { exact: true }), 28);
  const img = op.locator("img");
  await expect(img).toHaveAttribute("src", "/api/public/avatar?v=v1");
  await expect.poll(() => img.evaluate((i: HTMLImageElement) => i.complete && i.naturalWidth)).toBe(180);

  const visitorBlock = page.locator('[data-author="visitor"]');
  const v = visitorBlock.locator("[data-avatar]");
  await expectDiscBeside(v, visitorBlock.getByText("Jana · via public link"), 28);
  await expect(v).toHaveText("J");
  // A who-colour, painted (not a missing token falling back to transparent).
  expect(await v.evaluate((e) => getComputedStyle(e).backgroundColor)).not.toMatch(/rgba\(0, 0, 0, 0\)/);

  const agent = page.locator('[data-author="agent"]').first();
  await expectDiscBeside(agent.locator("[data-avatar]"), agent.getByText("Agent", { exact: true }), 28);
  await expect(agent.locator("[data-avatar]")).toHaveText("🤖");

  // The message text starts beside the disc, never under it or past the right edge.
  const text = visitorBlock.getByText("Does the embedding volume move with it?");
  const [t, d] = [await text.boundingBox(), await v.boundingBox()];
  expect(t!.x).toBeGreaterThanOrEqual(d!.x + d!.width);
  expect(t!.x + t!.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  await page.context().close();
});

async function operatorPage(page: Page, config: { avatar: string | null }) {
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "operator-csrf",
        new_session_engines: ["codex-api"],
        terminal_backend: "ws",
        auth_mode: "single-user",
        username: "marcus",
        default_project: "/home/u/proj",
        overview_expanded: [],
        projects_hidden: [],
        avatar: config.avatar,
      },
    }),
  );
  await page.route(/\/api\/profile\/avatar(\?.*)?$/, (r) =>
    r.request().method() === "GET"
      ? r.fulfill({ body: ICON, contentType: "image/png" })
      : r.fallback(),
  );
  const row = {
    id: KEY,
    engine: "codex-api",
    uuid: ID,
    short_uuid: ID.slice(0, 8),
    cwd: "/home/u/proj",
    project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
    last_mtime: Math.floor(Date.now() / 1000) - 60,
    first_user_message: "AI performance",
    title: "AI performance",
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
    if (/\/events(\?|$)/.test(r.request().url()))
      return r.fulfill({ json: { session_key: KEY, revision: 4, next_cursor: 4, events: [] } });
    return r.fulfill({
      json: {
        session_key: KEY,
        revision: 4,
        event_cursor: 4,
        cwd: "/home/u/proj",
        state: "idle",
        active_turn: null,
        model_requested: null,
        model_effective: "gpt-5-codex",
        turns: [
          { ...OPERATOR_TURN, operation_id: "", reason: null, ts: 1_800_000_000 },
          { ...VISITOR_TURN, operation_id: "", reason: null, ts: 1_800_000_050 },
        ],
        omitted_turns: 0,
        pending_requests: [],
        native: { native_id: "n1", worker: null, background_active: false },
        read_only: null,
        presence: { count: 2, visitors: [{ name: "Jana" }], shared: true },
      },
    });
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
}

test("the operator's API session: discs on every message and the head's connected count", async ({ page }) => {
  await operatorPage(page, { avatar: "v1" });
  await page.goto(`/s/codex-api/${ID}`);
  const turns = page.getByTestId("structured-turn");
  await expect(turns).toHaveCount(2);

  const chip = page.getByTestId("structured-present");
  await expect(chip.locator(".sr-only")).toHaveText("2 connected");
  await expect(chip.locator('span[aria-hidden="true"]')).toHaveText("2");
  await expect(chip).toBeInViewport();
  expect(await chip.getAttribute("title")).toContain("Jana · via public link");
  // Whole and on its own: not clipped to "2 connec", not drawn over the facts before it.
  expect(await chip.evaluate((e) => e.scrollWidth <= e.clientWidth)).toBe(true);
  const before = await chip.evaluate((e) => {
    const prev = e.previousElementSibling;
    if (!prev) return null;
    const range = document.createRange();
    range.selectNodeContents(prev);
    return range.getBoundingClientRect().right;
  });
  if (before != null) expect(before).toBeLessThanOrEqual((await chip.boundingBox())!.x + 0.5);

  const mine = turns.nth(0).locator("[data-avatar]");
  await expect(mine).toHaveCount(2); // you + the agent's reply
  await expectDiscBeside(mine.nth(0), turns.nth(0).getByText("you", { exact: true }), 24);
  const img = mine.nth(0).locator("img");
  await expect(img).toHaveAttribute("src", /^blob:/);
  await expect.poll(() => img.evaluate((i: HTMLImageElement) => i.complete && i.naturalWidth)).toBe(180);
  await expect(mine.nth(1)).toHaveText("🤖");

  const theirs = turns.nth(1).locator("[data-avatar]").first();
  await expectDiscBeside(theirs, turns.nth(1).getByText("Jana · via public link"), 24);
  await expect(theirs).toHaveText("J");
});

test("Settings › Profile: a chosen image is cropped square, scaled, uploaded, and shown", async ({ page }) => {
  let version: string | null = null;
  let uploaded: Buffer | null = null;
  let uploadedType = "";
  await operatorPage(page, { avatar: null });
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "operator-csrf",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "single-user",
        username: "marcus",
        avatar: version,
      },
    }),
  );
  await page.route(/\/api\/profile\/avatar(\?.*)?$/, async (r) => {
    const req = r.request();
    if (req.method() === "GET") return r.fulfill({ body: uploaded ?? ICON, contentType: uploadedType || "image/png" });
    if (req.headers()["x-csrf-token"] !== "operator-csrf") return r.fulfill({ status: 403, json: { detail: "csrf" } });
    if (req.method() === "POST") {
      // An edge WAF (OWASP CRS 920420) refuses a raw image/* request body before it reaches the
      // app — the operator saw only "forbidden". Model it: only multipart/form-data gets through.
      const ct = req.headers()["content-type"] ?? "";
      if (!ct.startsWith("multipart/form-data")) return r.fulfill({ status: 403, body: "forbidden" });
      const part = filePart(req.postDataBuffer()!, ct);
      if (!part) return r.fulfill({ status: 422, json: { detail: "no file in the form" } });
      uploaded = part.data;
      uploadedType = part.type;
      version = "v2";
      return r.fulfill({ json: { version } });
    }
    if (req.method() === "DELETE") {
      version = null;
      return r.fulfill({ json: { version: null } });
    }
    return r.fulfill({ status: 405, json: {} });
  });

  await page.goto(settingsPath("profile"));
  const panel = page.getByTestId("profile-settings");
  await expect(panel.locator('[data-avatar][data-kind="accent"]')).toHaveText("M");
  await panel.getByTestId("profile-avatar-input").setInputFiles(WIDE);
  const img = panel.locator("[data-avatar] img");
  // The bytes come through the fetch seam (the Home Free tunnel in app mode) as an object URL.
  await expect(img).toHaveAttribute("src", /^blob:/);
  await expect(panel.getByRole("alert")).toHaveCount(0);

  // What went over the wire is a square at most 256 px, encoded by the browser.
  expect(uploaded).not.toBeNull();
  expect(uploadedType).toMatch(/^image\/(webp|png)$/);
  const dims = await page.evaluate(async ([b64, type]) => {
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const bmp = await createImageBitmap(new Blob([bytes], { type }));
    return [bmp.width, bmp.height];
  }, [uploaded!.toString("base64"), uploadedType] as const);
  expect(dims).toEqual([256, 256]);
  await expect.poll(() => img.evaluate((i: HTMLImageElement) => i.complete && i.naturalWidth)).toBe(256);

  await panel.getByRole("button", { name: "Remove" }).click();
  await expect(panel.locator('[data-avatar][data-kind="accent"]')).toHaveText("M");
  await expect(panel.getByRole("button", { name: "Upload image" })).toBeVisible();
});

test("Settings › Profile: a display name is saved and becomes the disc's initial (#1942)", async ({
  page,
}, testInfo) => {
  let operatorName: string | null = null;
  const posted: unknown[] = [];
  await operatorPage(page, { avatar: null });
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "operator-csrf",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "single-user",
        username: "marcus",
        avatar: null,
        operator_name: operatorName,
      },
    }),
  );
  await page.route("**/api/prefs", async (r) => {
    const req = r.request();
    if (req.method() !== "POST") return r.fallback();
    if (req.headers()["x-csrf-token"] !== "operator-csrf") return r.fulfill({ status: 403, json: { detail: "csrf" } });
    const body = req.postDataJSON() as { operator_name?: string };
    posted.push(body);
    operatorName = body.operator_name || null;
    return r.fulfill({ json: { operator_name: operatorName } });
  });

  await page.goto(settingsPath("profile"));
  const panel = page.getByTestId("profile-settings");
  await expect(panel.locator('[data-avatar][data-kind="accent"]')).toHaveText("M"); // the login name's
  const field = panel.getByTestId("profile-display-name");
  await field.fill("Zoe Operator");
  await panel.getByRole("button", { name: "Save" }).click();
  await expect(panel.getByRole("status")).toHaveText("Saved.");
  expect(posted).toEqual([{ operator_name: "Zoe Operator" }]);
  await expect(panel.locator('[data-avatar][data-kind="accent"]')).toHaveText("Z");
  // The field and its Save button fit the viewport, and on a touch screen are touch-sized
  // (docs/design.md §8; the shared field is 40 px under a fine pointer by design).
  for (const el of [field, panel.getByRole("button", { name: "Save" })]) {
    const box = (await el.boundingBox())!;
    if (testInfo.project.use.isMobile) expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  }

  // Clearing it goes back to the login name's initial (and "Operator" for visitors).
  await field.fill("");
  await panel.getByRole("button", { name: "Save" }).click();
  await expect(panel.locator('[data-avatar][data-kind="accent"]')).toHaveText("M");
  expect(posted.at(-1)).toEqual({ operator_name: "" });
});
