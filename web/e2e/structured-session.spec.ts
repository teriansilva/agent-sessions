import { expect, test, type Page, type Route } from "@playwright/test";
import { mockRoster } from "./roster";

// #1311: a native API client's session is a STRUCTURED VIEW, never a terminal. The server is
// mocked: the snapshot route is a small state machine, so the spec drives the real client through
// a complete command approval, a review-only file change, a decision in flight, a lost connection,
// and starting a session from New session — on desktop AND mobile, in both themes. Long commands
// and patches must never scroll the page sideways, and every control keeps a 44px target.

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const TURN = "11111111-2222-4333-8444-555555555555";
const URL_PATH = `/s/codex-api/${ID}`;
const SESSION = new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}(/[a-z]+)?(\\?.*)?$`);

type Json = Record<string, unknown>;

function snapshot(over: Json = {}): Json {
  return {
    session_key: `codex-api:${ID}`,
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
    native: { native_id: "n1", worker: "w1", background_active: false },
    read_only: null,
    ...over,
  };
}

function turn(over: Json = {}): Json {
  return {
    turn_id: TURN,
    operation_id: TURN,
    state: "awaiting_approval",
    text: "Fix the flaky login spec and run it 20 times.",
    reply: "",
    text_truncated: false,
    reply_truncated: false,
    reason: null,
    tools: [{ id: "c1", name: "command", outcome: "", summary: "npx playwright test" }],
    tools_truncated: false,
    ...over,
  };
}

const LONG = "npx playwright test e2e/login.spec.ts " + "--grep=sign-in-redirect-under-load ".repeat(8);

async function setup(page: Page, theme: "dark" | "light", engines = ["codex-api"]) {
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: engines,
        unavailable_clients: [
          { id: "claude-api", label: "Claude — API", reason: "claude 2.1.287 or later is required for native mode" },
        ],
        terminal_backend: "ws",
        auth_mode: "none",
        default_project: "/home/u/proj",
        overview_expanded: [],
        projects_hidden: [],
        theme,
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
        api: { kind: "codex-app-server", source: "codex", unavailable_reason: null },
      },
    },
  });
}

/** Serve the session: `state()` is read on every snapshot; events report its revision. */
async function serveSession(
  page: Page,
  state: () => Json,
  handlers: { decide?: (r: Route, body: Json) => Promise<void> | void } = {},
) {
  await page.route(SESSION, async (r) => {
    const sub = r.request().url().match(SESSION)?.[1] ?? "";
    if (sub === "/events") {
      const s = state();
      return r.fulfill({ json: { session_key: s.session_key, revision: s.revision, next_cursor: s.revision, events: [] } });
    }
    if (sub === "/decisions" && handlers.decide) return handlers.decide(r, r.request().postDataJSON() as Json);
    if (sub === "") return r.fulfill({ json: state() });
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
}

async function noOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

async function targets(page: Page, scope: ReturnType<Page["getByTestId"]>) {
  for (const b of await scope.getByRole("button").all()) {
    const box = (await b.boundingBox())!;
    expect(box.height, `${await b.textContent()} is a 44px target`).toBeGreaterThanOrEqual(44);
  }
}

for (const theme of ["dark", "light"] as const) {
  test(`a command approval shows the whole request; approve once settles from the snapshot (${theme})`, async ({
    page,
  }) => {
    await setup(page, theme);
    let decided: Json | null = null;
    let state = snapshot({
      state: "awaiting_approval",
      active_turn: TURN,
      turns: [turn()],
      pending_requests: [
        {
          request_id: "7",
          turn_id: TURN,
          kind: "command",
          choices: ["approve", "reject"],
          complete: true,
          payload_digest: "3b0e9d2aa41c",
          payload: {
            command: LONG,
            cwd: "/home/u/proj/web",
            reason: "Run the login spec repeatedly to confirm the flake is gone.",
            networkApprovalContext: null,
            proposedExecpolicyAmendment: null,
            itemId: "call_9f2c",
          },
        },
      ],
    });
    await serveSession(page, () => state, {
      decide: async (r, body) => {
        decided = body;
        state = snapshot({ revision: 6, event_cursor: 6, turns: [turn({ state: "completed", reply: "20/20 passed." })] });
        await r.fulfill({ json: { decision: "approve" } });
      },
    });
    await page.goto(URL_PATH);
    const card = page.getByTestId("structured-request");
    await expect(card).toBeVisible();
    await expect(page.getByTestId("structured-pane")).toBeVisible();
    await expect(page.locator(".xterm")).toHaveCount(0);
    for (const field of ["command", "cwd", "reason", "networkApprovalContext", "proposedExecpolicyAmendment"]) {
      await expect(card.locator(`[data-field="${field}"]`)).toBeVisible();
    }
    await expect(card).toContainText("item Id call_9f2c");
    await expect(card.getByRole("button")).toHaveText(["Approve once", "Reject"]);
    await noOverflow(page);
    await targets(page, card);
    await card.getByRole("button", { name: "Approve once" }).click();
    await expect(page.getByTestId("structured-request")).toHaveCount(0);
    await expect(page.getByText("20/20 passed.")).toBeVisible();
    expect(decided).toMatchObject({ request_id: "7", turn_id: TURN, decision: "approve" });
  });

  test(`a file change is review-and-decline only; a long patch scrolls in its box, not the page (${theme})`, async ({
    page,
  }) => {
    await setup(page, theme);
    const diff = [
      "@@ -10,4 +10,4 @@",
      '   await page.getByRole("button", { name: "Sign in" }).click();',
      '+  await page.waitForURL("**/dashboard", { timeout: 15_000 }); // ' + "x".repeat(220),
      "-  await page.waitForTimeout(500);",
    ].join("\n");
    let decided: Json | null = null;
    await serveSession(
      page,
      () =>
        snapshot({
          state: "awaiting_approval",
          active_turn: TURN,
          turns: [turn()],
          pending_requests: [
            {
              request_id: "9",
              turn_id: TURN,
              kind: "file_change",
              choices: ["reject", "cancel"],
              complete: false,
              payload: { changes: [{ path: "web/e2e/login.spec.ts", diff }], reason: null },
            },
          ],
        }),
      {
        decide: async (r, body) => {
          decided = body;
          await r.fulfill({ json: {} });
        },
      },
    );
    await page.goto(URL_PATH);
    const card = page.getByTestId("structured-request");
    await expect(card.getByTestId("structured-patch")).toContainText("web/e2e/login.spec.ts");
    await expect(card.getByTestId("structured-review-only")).toBeVisible();
    await expect(card.getByRole("button", { name: /approve/i })).toHaveCount(0);
    await noOverflow(page);
    await targets(page, card);
    await card.getByRole("button", { name: "Decline" }).click();
    await expect.poll(() => decided).toMatchObject({ request_id: "9", decision: "reject" });
    // Still pending in the snapshot: the card says it is waiting, never that it is done.
    await expect(page.getByTestId("structured-deciding")).toContainText("waiting for");
  });
}

test("a decision in flight never looks settled, and its siblings wait", async ({ page }) => {
  await setup(page, "dark");
  await serveSession(
    page,
    () =>
      snapshot({
        state: "awaiting_approval",
        active_turn: TURN,
        turns: [turn()],
        pending_requests: [
          { request_id: "7", turn_id: TURN, kind: "command", choices: ["approve", "reject"], payload: { command: "ls" } },
        ],
      }),
    { decide: () => new Promise(() => {}) }, // the answer never comes
  );
  await page.goto(URL_PATH);
  await page.getByRole("button", { name: "Approve once" }).click();
  await expect(page.getByTestId("structured-deciding")).toContainText("Sending");
  await expect(page.getByRole("button", { name: "Reject" })).toBeDisabled();
  await expect(page.getByTestId("structured-request")).toBeVisible();
});

test("a lost connection keeps the turn and resumes from the cursor; nothing is re-sent", async ({ page }) => {
  await setup(page, "light");
  let offline = false;
  const posts: string[] = [];
  page.on("request", (req) => {
    if (req.method() === "POST") posts.push(req.url());
  });
  const running = snapshot({ state: "running", active_turn: TURN, turns: [turn({ state: "running", pending_requests: [] })] });
  await page.route(SESSION, (r) => {
    if (offline) return r.abort("internetdisconnected");
    const sub = r.request().url().match(SESSION)?.[1] ?? "";
    if (sub === "/events") return r.fulfill({ json: { session_key: "", revision: 4, next_cursor: 4, events: [] } });
    return r.fulfill({ json: running });
  });
  await page.goto(URL_PATH);
  await expect(page.getByTestId("structured-working")).toBeVisible();
  offline = true;
  await expect(page.getByTestId("structured-reconnecting")).toContainText("event 4");
  await expect(page.getByTestId("structured-worker")).toContainText("reconnecting");
  offline = false;
  await expect(page.getByTestId("structured-reconnecting")).toHaveCount(0, { timeout: 20_000 });
  expect(posts).toEqual([]);
  await noOverflow(page);
});

test("New session: API clients are their own group, unavailable ones say why, no bypass; Start creates on the server", async ({
  page,
}) => {
  await setup(page, "dark", ["claude", "codex-api"]);
  let created: Json | null = null;
  await page.route("**/api/structured/sessions", async (r) => {
    created = r.request().postDataJSON() as Json;
    await r.fulfill({ status: 201, json: snapshot() });
  });
  await serveSession(page, () => snapshot());
  await page.goto("/");
  const agent = page.getByRole("combobox", { name: "Agent" });
  await expect(agent.locator("optgroup")).toHaveCount(2);
  await expect(agent.locator('option[disabled]')).toHaveText("Claude — API — unavailable");
  await expect(page.getByTestId("new-session-unavailable")).toContainText("2.1.287 or later");
  await expect(page.getByRole("checkbox", { name: /skip permission prompts/i })).toBeVisible();
  await agent.selectOption("codex-api");
  await expect(page.getByTestId("new-session-api-about")).toContainText("Codex");
  await expect(page.getByRole("checkbox", { name: /skip permission prompts/i })).toHaveCount(0);
  await noOverflow(page);
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(new RegExp(`/s/codex-api/${ID}$`));
  expect(created).toMatchObject({ engine: "codex-api", cwd: "/home/u/proj" });
  expect(Object.keys(created!).sort()).toEqual(["cwd", "engine", "operation_id"]);
  await expect(page.getByTestId("structured-empty")).toBeVisible();
});

const MD_REPLY = [
  "## Result",
  "",
  "The spec is **green** after `retries: 0`:",
  "",
  "- 20/20 runs passed",
  "- no new warnings",
  "",
  "```ts",
  `await page.goto("/login"); // ${"x".repeat(160)}`,
  "```",
  "",
  "See [the run](https://example.com/run/1) — <script>window.__pwned = 1</script>",
].join("\n");

for (const theme of ["dark", "light"] as const) {
  test(`an agent reply renders as Markdown, inert to HTML, without widening the page (#1332, ${theme})`, async ({
    page,
  }) => {
    await setup(page, theme);
    await serveSession(page, () =>
      snapshot({ turns: [turn({ state: "completed", tools: [], reply: MD_REPLY })] }),
    );
    await page.goto(URL_PATH);
    const reply = page.getByTestId("structured-turn").locator("h2", { hasText: "Result" });
    await expect(reply).toBeVisible();
    const turnEl = page.getByTestId("structured-turn");
    await expect(turnEl.locator("strong")).toHaveText("green");
    await expect(turnEl.locator("p code")).toHaveText("retries: 0");
    await expect(turnEl.locator("ul > li")).toHaveCount(2);
    const code = page.getByTestId("md-code");
    await expect(code).toContainText("ts");
    await expect(code.locator("pre code")).toContainText('await page.goto("/login");');
    const link = turnEl.getByRole("link", { name: "the run" });
    await expect(link).toHaveAttribute("href", "https://example.com/run/1");
    await expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
    await expect(turnEl.locator("script")).toHaveCount(0);
    await expect(turnEl).not.toContainText("**");
    await noOverflow(page);
    await targets(page, code);
  });
}

test("New session: an API client offers the models its CLI reports and creates on the chosen one (#1313)", async ({
  page,
}) => {
  await setup(page, "dark", ["codex-api"]);
  let created: Json | null = null;
  await page.route("**/api/structured/clients/codex-api/models", (r) =>
    r.fulfill({
      json: {
        status: "ok",
        reason: null,
        models: [
          { id: "gpt-6.1-sol", label: "GPT-6.1 Sol", description: null, efforts: [], is_default: true },
          { id: "gpt-6-luna", label: "gpt-6-luna", description: null, efforts: [], is_default: false },
        ],
      },
    }),
  );
  await page.route("**/api/structured/sessions", async (r) => {
    created = r.request().postDataJSON() as Json;
    await r.fulfill({ status: 201, json: snapshot() });
  });
  await serveSession(page, () => snapshot());
  await page.goto("/");
  const model = page.getByRole("combobox", { name: "Model" });
  await expect(model.locator("option")).toHaveText(["default", "GPT-6.1 Sol (gpt-6.1-sol)", "gpt-6-luna"]);
  await model.selectOption("gpt-6.1-sol");
  await noOverflow(page);
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(new RegExp(`/s/codex-api/${ID}$`));
  expect(created).toMatchObject({ engine: "codex-api", model: "gpt-6.1-sol" });
});

// ---- the pane head and Files (#1332 Phase 2) ---------------------------------------------------

const ROW = {
  id: `codex-api:${ID}`,
  engine: "codex-api",
  uuid: ID,
  short_uuid: ID.slice(0, 8),
  cwd: "/home/u/proj",
  project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
  last_mtime: Math.floor(Date.now() / 1000) - 60,
  first_user_message: "Fix the flaky login spec",
  title: "Fix the flaky login spec",
  sticky: false,
  archived: false,
  ai_summary: "",
};

async function serveRowAndFiles(page: Page) {
  await page.route("**/api/sessions?**", (r) =>
    r.fulfill({ json: { sessions: [ROW], next_offset: null, total: 1, facets: { projects: [], engines: [] } } }),
  );
  await page.route(new RegExp(`/api/sessions/codex-api(?::|%3A)${ID}$`), (r) => r.fulfill({ json: ROW }));
  await page.route("**/api/files/capabilities", (r) => r.fulfill({ json: { ok: true, reason: "" } }));
  await page.route("**/api/files/list**", (r) =>
    r.fulfill({
      json: {
        path: ROW.cwd,
        parent: "/home/u",
        root: "/home/u",
        entries: [{ name: "app.py", path: `${ROW.cwd}/app.py`, kind: "file", size: 10, mtime: 0 }],
        total: 1,
        complete: true,
        truncated: false,
      },
    }),
  );
}

/** Run a head action wherever the head put it: an inline chip, the measured fold's "…" menu, or
 *  (≤800px) the ONE Actions menu. Menu items keep the action's accessible name. */
async function headAction(page: Page, aria: string) {
  const pane = page.getByTestId("structured-pane");
  const trigger = pane.getByTestId("head-actions-menu");
  const chip = pane.locator("[data-head-action]").first();
  await expect(trigger.or(chip).first()).toBeVisible();
  // The fold re-measures after the first paint, so a chip seen inline can move into the "…"
  // menu before the click lands: retry the whole lookup until one path runs the action.
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
  }).toPass({ timeout: 20_000 });
}

test("the API pane carries the terminal's head actions, and Files opens the drawer at the session's folder (#1332)", async ({
  page,
}, testInfo) => {
  await setup(page, "dark");
  await serveRowAndFiles(page);
  await serveSession(page, () => snapshot({ turns: [turn({ state: "completed", tools: [], reply: "done" })] }));
  await page.goto(URL_PATH);
  const pane = page.getByTestId("structured-pane");
  await expect(pane.getByTestId("structured-turn")).toBeVisible();
  if (testInfo.project.name === "mobile") {
    // ≤800px: ONE Actions menu carries every action; a phone never hosts a window, so no To map.
    await expect(pane.locator("[data-head-action]")).toHaveCount(0);
    // The trigger never overlaps the identity run beside it (a squeezed box let it sit on the title).
    const t = (await pane.getByTestId("head-actions-menu").boundingBox())!;
    const title = (await pane.getByText("Codex", { exact: true }).first().boundingBox())!;
    expect(t.x).toBeGreaterThanOrEqual(title.x + title.width);
    // The worker chip shows its LED only on a phone, and nothing in it is clipped.
    const worker = pane.getByTestId("structured-worker");
    await expect(worker).toContainText("worker live"); // still its accessible text
    expect(await worker.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(0);
    await pane.getByTestId("head-actions-menu").click();
    await expect(page.getByRole("menuitem")).toHaveText(["Files", "Recap", "Hand off", "Share link"]);
    await page.keyboard.press("Escape");
  } else {
    const labels = await pane.locator("[data-head-action]").evaluateAll((els) =>
      els.map((e) => e.getAttribute("aria-label")),
    );
    expect(labels.slice(0, 3)).toEqual([
      "Browse session files",
      "Open session brief",
      "Hand off session to another engine",
    ]);
  }
  await expect(page.getByRole("button", { name: /adopt this session/i })).toHaveCount(0);
  await headAction(page, "Browse session files");
  await expect(page.locator("[data-file-row]", { hasText: "app.py" })).toBeVisible();
  // No Compose draft to add a path to until #1332 Phase 3 — the action is not offered.
  await expect(page.locator("[data-send-path]")).toHaveCount(0);
  await noOverflow(page);
});

test("Share link copies the API session's URL (#1332)", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.addInitScript(() => {
    Object.defineProperty(Navigator.prototype, "share", { value: undefined, configurable: true });
  });
  await setup(page, "light");
  await serveRowAndFiles(page);
  await serveSession(page, () => snapshot());
  await page.goto(URL_PATH);
  await expect(page.getByTestId("structured-empty")).toBeVisible();
  await headAction(page, "Share a link to this session");
  await expect(page.locator("[data-link-toast]")).toHaveText("Link copied");
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toBe(new URL(URL_PATH, page.url()).toString());
});

test("in a map window the API pane has no bar of its own: chips in the chrome, ONE ⋯, Files in the window (#1332, #1109)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "windows are desktop-only (#208)");
  await setup(page, "dark");
  await serveRowAndFiles(page);
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["codex-api"],
        unavailable_clients: [],
        terminal_backend: "ws",
        auth_mode: "none",
        default_project: "/home/u/proj",
        overview_expanded: ["project:p1"],
        projects_hidden: [],
        project_names: {},
        theme: "dark",
      },
    }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) =>
    r.fulfill({ json: { projects: [{ id: "p1", name: "proj", color: "#ffb000", archived: false }] } }),
  );
  await serveSession(page, () => snapshot({ turns: [turn({ state: "completed", tools: [], reply: "done" })] }));
  await page.goto("/overview");
  await page.locator(".tr-ov-chip").first().click();
  const win = page.locator(`[data-session-window="codex-api:${ID}"]`);
  await expect(win.getByTestId("structured-turn")).toBeVisible();
  // The chrome bar is the window's ONLY bar: the pane's own head (worker chip, Stop) is gone.
  await expect(win.getByTestId("structured-worker")).toHaveCount(0);
  await expect(win.locator("[data-window-head]")).toHaveCount(1);
  // ONE menu: the chrome's ⋯, never a second "…" of the pane's own.
  await expect(win.locator("[data-window-menu]")).toHaveCount(1);
  await expect(win.getByRole("button", { name: "More session actions" })).toHaveCount(0);
  const slot = win.locator("[data-window-actions-slot]");
  // ONE action, ONE name (#1329): Recap and Hand off appear once across the chips and the ⋯ —
  // with chips inline at the opening width, and again with everything folded at the floor.
  const named = async () => {
    const chips = await slot
      .locator("[data-head-action]")
      .evaluateAll((els) => els.map((e) => `${e.getAttribute("aria-label")} ${e.textContent}`));
    await win.locator("[data-window-menu]").click();
    const menu = page.locator("[role='menu'][aria-label='Session actions']").last();
    await expect(menu).toBeVisible();
    const items = await menu
      .locator("[role='menuitem']")
      .evaluateAll((els) => els.map((e) => `${e.getAttribute("aria-label")} ${e.textContent}`));
    await page.keyboard.press("Escape");
    await expect(menu).toBeHidden();
    const all = [...chips, ...items];
    expect(all.filter((n) => /brief|recap/i.test(n))).toHaveLength(1);
    expect(all.filter((n) => /hand off/i.test(n))).toHaveLength(1);
    return chips.length;
  };
  const inline = await named();
  await win.locator("[data-window-resize]").focus();
  for (let i = 0; i < 20; i++) await page.keyboard.press("Shift+ArrowLeft");
  await expect.poll(async () => slot.locator("[data-head-action]").count()).toBeLessThan(inline);
  await named();

  const chip = slot.locator("[data-head-action='files']");
  if (await chip.isVisible()) await chip.click();
  else {
    await win.locator("[data-window-menu]").click();
    await page.getByRole("menuitem", { name: "Browse session files" }).click();
  }
  await expect(win.locator("[data-window-files-drawer]")).toBeVisible();
  await expect(win.locator("[data-file-row]", { hasText: "app.py" })).toBeVisible();
  // An API pane has no Compose draft yet (#1332 Phase 3), so Files offers no "add to message".
  await expect(win.locator("[data-send-path]")).toHaveCount(0);
});

// ---- pictures in a send (#1332 Phase 3) --------------------------------------------------------

/** A 96×64 checked PNG: big enough that the thumbnail is visibly a picture. */
const PNG_B64 = [
  "iVBORw0KGgoAAAANSUhEUgAAAGAAAABACAIAAABqVuVZAAAAi0lEQVR42u3aMRHAMAwDQMPJWBQFm6kMUwzacvbfCY",
  "D0s+p8FWWtJ8q7T5Tb+hQgQIAAAQIECBAgQIAAAQIECFAzoGmD0z6AAAECBAgQIECAAAECBAgQIEDdgKYNTvsAAgQI",
  "ECBAgAABAgQIECBAgAB1A3Kg8jADBAgQIECAAAECBAgQIECAAI0C+gHzA/HhyUVm9wAAAABJRU5ErkJggg==",
].join("");
const STORED = "20261008-010000-shot.png";

for (const theme of ["dark", "light"] as const) {
  test(`paste an image → it shows as a chip → send carries it → the turn shows the thumbnail (#1332, ${theme})`, async ({
    page,
  }) => {
    await setup(page, theme);
    let posted: Json | null = null;
    await page.route("**/api/upload", (r) =>
      r.fulfill({ json: { path: `/home/u/.agent-sessions/uploads/${STORED}`, name: "shot.png", stored: STORED } }),
    );
    await page.route(`**/api/uploads/${STORED}`, (r) =>
      r.fulfill({ body: Buffer.from(PNG_B64, "base64"), contentType: "image/png" }),
    );
    await serveSession(page, () =>
      posted
        ? snapshot({
            images: true,
            revision: 5,
            turns: [
              turn({
                turn_id: posted.operation_id,
                operation_id: posted.operation_id,
                state: "completed",
                text: posted.text,
                reply: "The **button** is clipped.",
                tools: [],
                attachments: [{ stored: STORED, mime: "image/png" }],
              }),
            ],
          })
        : snapshot({ images: true }),
    );
    // After serveSession: the newest route wins, and its pattern also matches `/turns`.
    await page.route(new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}/turns$`), async (r) => {
      posted = r.request().postDataJSON() as Json;
      await r.fulfill({ status: 202, json: { state: "running" } });
    });
    await page.goto(URL_PATH);
    const box = page.getByRole("textbox", { name: /Message/ });
    await expect(box).toBeVisible();
    await box.evaluate((el, b64) => {
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      const dt = new DataTransfer();
      dt.items.add(new File([bytes], "shot.png", { type: "image/png" }));
      el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
    }, PNG_B64);
    const chip = page.getByTestId("structured-attachment");
    await expect(chip).toContainText("shot.png");
    await expect(chip.locator("img")).toHaveJSProperty("complete", true);
    // The picture's slot never squeezes the name: "shot.png" fits whole beside its thumbnail.
    expect(await chip.getByText("shot.png").evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(0);
    await noOverflow(page);
    await targets(page, page.getByRole("list", { name: "Attached images" }));
    await box.fill("what is clipped here?");
    await page.getByRole("button", { name: "Send" }).click();
    await expect.poll(() => posted).not.toBeNull();
    expect(posted).toMatchObject({ text: "what is clipped here?", attachments: [STORED] });
    const sent = page.getByTestId("structured-turn");
    const thumb = sent.getByRole("img", { name: "Attached image" });
    await expect(thumb).toBeVisible();
    expect(await thumb.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(96);
    expect((await thumb.boundingBox())!.width).toBeGreaterThanOrEqual(90); // shown, not a dot
    await expect(chip).toHaveCount(0);
    await noOverflow(page);
  });
}

test("a client that takes no pictures shows no attach control (#1332)", async ({ page }) => {
  await setup(page, "dark");
  await serveSession(page, () => snapshot({ images: false }));
  await page.goto(URL_PATH);
  await expect(page.getByRole("textbox", { name: /Message/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Attach images" })).toHaveCount(0);
});

// ---- templates and sent history (#1332 Phase 3b) -----------------------------------------------

for (const theme of ["dark", "light"] as const) {
  test(`a template inserts into the message, sends with its picture, and Sent restores it (#1332, ${theme})`, async ({
    page,
  }) => {
    await setup(page, theme);
    await page.addInitScript(() => localStorage.removeItem("as:sent:v1"));
    let posted: Json | null = null;
    await page.route("**/api/template-variables", (r) => r.fulfill({ json: { variables: [], limits: {} } }));
    await page.route("**/api/templates", (r) =>
      r.fulfill({
        json: {
          templates: [
            {
              id: "repro",
              name: "Repro steps",
              description: "Reproduce a UI bug",
              tags: [],
              body: "Reproduce {{what}} in a real browser",
              fields: [{ name: "what", label: "What", default: "", required: true }],
              images: [{ name: "shot.png", path: `/home/u/.agent-sessions/uploads/${STORED}` }],
              created_at: 1,
              updated_at: 1,
              used_count: 0,
              last_used_at: null,
            },
          ],
        },
      }),
    );
    await page.route(`**/api/uploads/${STORED}`, (r) =>
      r.fulfill({ body: Buffer.from(PNG_B64, "base64"), contentType: "image/png" }),
    );
    await serveSession(page, () =>
      posted
        ? snapshot({
            images: true,
            revision: 5,
            turns: [
              turn({
                turn_id: posted.operation_id,
                operation_id: posted.operation_id,
                state: "completed",
                text: posted.text,
                reply: "Reproduced.",
                tools: [],
                attachments: [{ stored: STORED, mime: "image/png" }],
              }),
            ],
          })
        : snapshot({ images: true }),
    );
    await page.route(new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}/turns$`), async (r) => {
      posted = r.request().postDataJSON() as Json;
      await r.fulfill({ status: 202, json: { state: "running" } });
    });
    await page.goto(URL_PATH);
    const tools = page.getByRole("toolbar", { name: "Message tools" });
    await expect(tools).toBeVisible();
    await targets(page, tools);
    await noOverflow(page);
    await tools.getByRole("button", { name: /templates/i }).click();
    await page.getByText("Repro steps").click();
    await page.getByRole("textbox", { name: /^What/ }).fill("the logout bug");
    await page.getByRole("button", { name: "Insert Repro steps into message" }).click();
    const box = page.getByRole("textbox", { name: /Message/ });
    await expect(box).toHaveValue("Reproduce the logout bug in a real browser");
    await expect(page.getByTestId("structured-attachment")).toHaveCount(1);
    await page.getByRole("button", { name: "Send" }).click();
    await expect.poll(() => posted).not.toBeNull();
    expect(posted).toMatchObject({ text: "Reproduce the logout bug in a real browser", attachments: [STORED] });
    await expect(box).toHaveValue("");
    await tools.getByRole("button", { name: /^Sent$/ }).click();
    await page.getByRole("button", { name: /restore/i }).first().click();
    await expect(box).toHaveValue("Reproduce the logout bug in a real browser");
    await expect(page.getByTestId("structured-attachment")).toHaveCount(1);
    await noOverflow(page);
  });
}
