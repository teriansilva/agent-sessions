import { expect, test, type Page, type Route } from "@playwright/test";
import { mockRoster } from "./roster";
import directoryGrants from "../../tests/fixtures/claude_directory_grants.json" with { type: "json" };

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
        api: { kind: "codex-app-server", source: "codex", unavailable_reason: null, can_bypass: true },
      },
    },
  });
}

/** Serve the session: `state()` is read on every snapshot; events report its revision. */
async function serveSession(
  page: Page,
  state: () => Json,
  handlers: {
    decide?: (r: Route, body: Json) => Promise<void> | void;
    start?: (r: Route) => Promise<void> | void;
  } = {},
) {
  await page.route(SESSION, async (r) => {
    const sub = r.request().url().match(SESSION)?.[1] ?? "";
    if (sub === "/events") {
      const s = state();
      return r.fulfill({ json: { session_key: s.session_key, revision: s.revision, next_cursor: s.revision, events: [] } });
    }
    if (sub === "/decisions" && handlers.decide) return handlers.decide(r, r.request().postDataJSON() as Json);
    if (sub === "/start" && handlers.start) return handlers.start(r);
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

/** A guarded opencode API attempt from an earlier visit; choosing an agent is read-only. */
async function recoverOpencode(page: Page, theme: "dark" | "light") {
  await setup(page, theme, ["opencode", "opencode-api", "codex-api", "claude-api", "apichat"]);
  await mockRoster(page, {
    overrides: {
      "opencode-api": {
        present: true,
        supports_new: true,
        api: { kind: "opencode-acp", source: "opencode", unavailable_reason: null },
      },
    },
  });
  const mutations: { method: string; url: string }[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/structured/sessions") && request.method() !== "GET") {
      mutations.push({ method: request.method(), url: request.url() });
    }
  });
  await page.route(/\/api\/structured\/sessions\/opencode-api(?::|%3A)[0-9a-f-]+(\/[a-z]+)?(\?.*)?$/, (r) =>
    r.fulfill({ json: snapshot({ session_key: `opencode-api:${ID}`, native: null }) }),
  );
  await page.goto("/");
  await page.evaluate((id) => sessionStorage.setItem("battlelab.pendingStructuredCreate", JSON.stringify({
    engine: "opencode-api", cwd: "/home/u/proj", model: "default", id,
  })), ID);
  await page.reload();
  await page.getByRole("combobox", { name: "Agent", exact: true }).selectOption("opencode-api");
  await expect(page.getByTestId("api-recovered-create")).toBeVisible();
  return mutations;
}

for (const theme of ["dark", "light"] as const) {
  test(`New session: API recovery stays with its client; dismiss keeps history and permits a fresh start (${theme})`, async ({ page }) => {
    const mutations = await recoverOpencode(page, theme);
    const agent = page.getByRole("combobox", { name: "Agent", exact: true });
    const notice = page.getByTestId("api-recovered-create");
    // #1373: the saved API attempt leaked into the ordinary terminal and every other client.
    for (const engine of ["opencode", "codex-api", "claude-api", "apichat"]) {
      await agent.selectOption(engine);
      await expect(notice).toHaveCount(0);
    }
    expect(JSON.parse((await page.evaluate(() => sessionStorage.getItem("battlelab.pendingStructuredCreate")))!).id).toBe(ID);
    await agent.selectOption("opencode-api");
    await expect(notice).toContainText("A previous opencode — API session is saved");
    await expect(notice).toContainText("Open it to check its status");
    await noOverflow(page);
    const dismiss = notice.getByRole("button", { name: "Dismiss reminder" });
    const open = notice.getByRole("link", { name: "Open previous session" });
    for (const action of [dismiss, open]) {
      expect((await action.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    }
    await open.focus();
    await page.keyboard.press("Tab");
    await expect(dismiss).toBeFocused();
    await expect(dismiss).toHaveCSS("outline-style", "solid");
    await expect(dismiss).toHaveCSS("outline-width", "2px");
    await page.keyboard.press("Shift+Tab");
    await expect(open).toBeFocused();
    await expect(open).toHaveCSS("outline-width", "2px");
    expect(mutations).toEqual([]);
    await dismiss.click();
    await expect(notice).toHaveCount(0);
    expect(await page.evaluate(() => sessionStorage.getItem("battlelab.pendingStructuredCreate"))).toBeNull();
    expect(mutations).toEqual([]); // no stop, delete, create, or start
    await page.reload();
    await agent.selectOption("opencode-api");
    await expect(notice).toHaveCount(0);
    expect(mutations).toEqual([]);
    const bodies: Json[] = [];
    await page.route("**/api/structured/sessions", (r) => {
      const body = r.request().postDataJSON() as Json;
      bodies.push(body);
      return r.fulfill({ status: 201, json: snapshot({ session_key: `opencode-api:${String(body.operation_id)}` }) });
    });
    await page.getByRole("button", { name: /start session/i }).click();
    await expect.poll(() => bodies.length).toBe(1);
    expect(bodies[0].operation_id).not.toBe(ID);
    expect(bodies[0]).not.toHaveProperty("bypass"); // guarded requests omit the permission override
    await expect(page).toHaveURL(new RegExp(`/s/opencode-api/${String(bodies[0].operation_id)}$`));
    expect(mutations).toHaveLength(1);
  });
}

test("New session: opening recovery during a retry keeps its identity after a lost response (#1373)", async ({ page }) => {
  await recoverOpencode(page, "dark");
  const bodies: Json[] = [];
  let release!: () => void;
  await page.route("**/api/structured/sessions", async (r) => {
    bodies.push(r.request().postDataJSON() as Json);
    if (bodies.length === 1) {
      await new Promise<void>((resolve) => { release = resolve; });
      return r.abort("connectionreset");
    }
    return r.fulfill({ status: 201, json: snapshot({ session_key: `opencode-api:${ID}` }) });
  });
  await page.getByRole("button", { name: /start session/i }).click();
  await expect.poll(() => bodies.length).toBe(1);
  const lost = page.waitForEvent("requestfailed", (r) => r.method() === "POST" && r.url().endsWith("/api/structured/sessions"));
  await page.getByRole("link", { name: "Open previous session" }).click();
  await expect(page).toHaveURL(new RegExp(`/s/opencode-api/${ID}$`));
  const stored = await page.evaluate(() => sessionStorage.getItem("battlelab.pendingStructuredCreate"));
  release();
  await lost;
  expect(stored).not.toBeNull();
  expect(JSON.parse(stored!).id).toBe(ID);
  await page.goto("/");
  await page.getByRole("combobox", { name: "Agent", exact: true }).selectOption("opencode-api");
  await expect(page.getByTestId("api-recovered-create")).toBeVisible();
  await page.getByRole("button", { name: /start session/i }).click();
  await expect.poll(() => bodies.length).toBe(2);
  expect(bodies.map((body) => body.operation_id)).toEqual([ID, ID]);
});
for (const engine of ["codex-api", "claude-api", "opencode-api"]) {
  test(`${engine}: sends follow-ups while working without interrupting (#1378)`, async ({ page }) => {
    await setup(page, "dark", [engine]);
    await mockRoster(page, { overrides: { [engine]: { present: true, supports_new: true } } });
    const session = new RegExp(`/api/structured/sessions/${engine}(?::|%3A)${ID}(/[a-z]+)?(\\?.*)?$`);
    const running = turn({ state: "running", reply: "Checking…" });
    const turns: Json[] = [running];
    let revision = 4;
    const mutations: string[] = [];
    await page.route(session, async (r) => {
      const sub = r.request().url().match(session)?.[1] ?? "";
      if (r.request().method() === "POST") mutations.push(sub);
      if (sub === "/turns") {
        const body = r.request().postDataJSON() as Json;
        const queued = turn({ turn_id: body.operation_id, operation_id: body.operation_id,
          text: body.text, state: "queued", reply: "", tools: [] });
        turns.push(queued); revision++;
        return r.fulfill({ json: queued });
      }
      if (sub === "/events") return r.fulfill({ json: { revision, next_cursor: revision, events: [] } });
      return r.fulfill({ json: snapshot({ session_key: `${engine}:${ID}`, revision, event_cursor: revision,
        state: "running", active_turn: TURN, turns }) });
    });
    await page.goto(`/s/${engine}/${ID}`);
    const box = page.getByRole("textbox", { name: /^Message / });
    await box.fill("Check parallel sessions too");
    await expect(page.getByRole("button", { name: "Send", exact: true })).toBeEnabled();
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(page.getByTestId("structured-queued")).toHaveCount(1);
    await expect(box).toHaveValue("");
    await box.fill("Include all API agents");
    await box.press("Enter");
    await expect(page.getByTestId("structured-queued")).toHaveCount(2);
    await page.reload();
    await expect(page.getByTestId("structured-queued")).toHaveCount(2);
    expect(mutations).toEqual(["/turns", "/turns"]);
    await expect(page.getByRole("button", { name: "Interrupt", exact: true })).toHaveCount(1);
    await noOverflow(page);
  });
}


async function targets(page: Page, scope: ReturnType<Page["getByTestId"]>) {
  for (const b of await scope.getByRole("button").all()) {
    const box = (await b.boundingBox())!;
    expect(box.height, `${await b.textContent()} is a 44px target`).toBeGreaterThanOrEqual(44);
  }
}

/** The composer is the terminal's (#1348): its chips are the terminal composer's chip — one row,
 *  one height, square — and Send is its amber CTA, never the old chat-pane buttons. */
async function terminalChips(form: ReturnType<Page["getByTestId"]>) {
  await expect(form.locator("[data-key]").first()).toBeVisible(); // the chips follow the snapshot
  const chips = await form.locator("[data-key]").all();
  const send = form.getByRole("button", { name: "Send" });
  const row = (await send.boundingBox())!;
  for (const c of chips) {
    const b = (await c.boundingBox())!;
    expect(b.height).toBe(32);
    expect(Math.abs(b.y + b.height / 2 - (row.y + row.height / 2))).toBeLessThanOrEqual(1); // one row
    expect(await c.evaluate((el) => getComputedStyle(el).borderRadius)).toBe("0px");
  }
  // The text box sits ABOVE the row, as in the terminal.
  const ta = (await form.getByRole("textbox").boundingBox())!;
  expect(ta.y + ta.height).toBeLessThanOrEqual(row.y);
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
  // The head's link LED says it, as a terminal's does for its socket (#1348).
  await expect(page.locator("[data-panel-head] [data-head-led]")).toHaveAttribute("data-head-led", "reconnecting");
  offline = false;
  await expect(page.getByTestId("structured-reconnecting")).toHaveCount(0, { timeout: 20_000 });
  expect(posts).toEqual([]);
  await noOverflow(page);
});

test("New session: API clients are their own group, unavailable ones say why; a guarded Start creates on the server", async ({
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
  // #1339: the toggle is offered for an API client whose adapter maps it; unticked = guarded.
  const skip = page.getByRole("checkbox", { name: /skip permission prompts/i });
  await expect(skip).toBeVisible();
  await skip.uncheck();
  await expect(page.getByTestId("api-bypass-warning")).toHaveCount(0);
  await noOverflow(page);
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(new RegExp(`/s/codex-api/${ID}$`));
  expect(created).toMatchObject({ engine: "codex-api", cwd: "/home/u/proj" });
  // A guarded create is the same request it always was: no `bypass` key at all.
  expect(Object.keys(created!).sort()).toEqual(["cwd", "engine", "operation_id"]);
  await expect(page.getByTestId("structured-empty")).toBeVisible();
  // The info screen (#1348): what this session is, never the old "No terminal: …" paragraph.
  const info = page.getByRole("region", { name: "Session info" });
  await expect(info).toContainText("/home/u/proj");
  await expect(info.getByTestId("structured-worker")).toBeVisible();
  await expect(page.getByText(/no terminal/i)).toHaveCount(0);
  await terminalChips(page.getByRole("form", { name: "Compose message" }));
  await noOverflow(page);
  await expect(page.getByTestId("structured-bypass")).toHaveCount(0);
});

test("New session: Skip permission prompts creates, THEN starts the session — and the head says so (#1339)", async ({
  page,
}) => {
  await setup(page, "dark", ["claude", "codex-api"]);
  const calls: string[] = [];
  let created: Json | null = null;
  let started = false;
  await page.route("**/api/structured/sessions", async (r) => {
    calls.push("create");
    created = r.request().postDataJSON() as Json;
    await r.fulfill({ status: 201, json: snapshot({ bypass: true, pending_start: true, native: null }) });
  });
  await serveSession(page, () => snapshot({ bypass: true, pending_start: !started }), {
    start: (r) => {
      calls.push("start");
      started = true;
      return r.fulfill({ json: snapshot({ bypass: true }) });
    },
  });
  await page.goto("/");
  await page.getByRole("combobox", { name: "Agent" }).selectOption("codex-api");
  const skip = page.getByRole("checkbox", { name: /skip permission prompts/i });
  await skip.check();
  await expect(page.getByTestId("api-bypass-warning")).toContainText("won’t ask");
  await noOverflow(page);
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(new RegExp(`/s/codex-api/${ID}$`));
  expect(created).toMatchObject({ engine: "codex-api", cwd: "/home/u/proj", bypass: true });
  expect(calls).toEqual(["create", "start"]); // launched only after the create's response
  await expect(page.getByTestId("structured-bypass")).toBeVisible();
  await expect(page.getByTestId("structured-bypass")).toHaveText(/skip permissions/i);
  await expect(page.getByTestId("structured-pending-start")).toHaveCount(0);
  await noOverflow(page);
});

test("An unstarted skip session (its start was lost) offers Start or Discard; Start launches it (#1339)", async ({
  page,
}) => {
  await setup(page, "dark");
  let started = false;
  let starts = 0;
  await serveSession(page, () => snapshot({ bypass: true, pending_start: !started, ...(started ? {} : { native: null }) }), {
    start: (r) => {
      starts += 1;
      started = true;
      return r.fulfill({ json: snapshot({ bypass: true }) });
    },
  });
  await page.goto(`/s/codex-api/${ID}`);
  const panel = page.getByTestId("structured-pending-start");
  await expect(panel).toContainText("Nothing has run");
  await expect(page.getByRole("textbox", { name: /message/i })).toBeDisabled();
  await noOverflow(page);
  await panel.getByRole("button", { name: "Start (skips prompts)" }).click();
  await expect(panel).toHaveCount(0);
  expect(starts).toBe(1);
  await expect(page.getByRole("textbox", { name: /message/i })).toBeEnabled();
});

test("New session: a console skip choice never carries into an API client (Hermes on #1341)", async ({
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
  const skip = page.getByRole("checkbox", { name: /skip permission prompts/i });
  await skip.uncheck(); // the console agent: off…
  await skip.check(); // …and explicitly on again
  await page.getByRole("combobox", { name: "Agent" }).selectOption("codex-api");
  await expect(skip).not.toBeChecked();
  await expect(page.getByTestId("api-bypass-warning")).toHaveCount(0);
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(new RegExp(`/s/codex-api/${ID}$`));
  expect(Object.keys(created!).sort()).toEqual(["cwd", "engine", "operation_id"]);
});

test("New session: a lost skip create keeps no client slot — the error points at the session list; it waits there with Start (5913)", async ({
  page,
}) => {
  await setup(page, "dark", ["claude", "codex-api"]);
  const bodies: Json[] = [];
  await page.route("**/api/structured/sessions", async (r) => {
    bodies.push(r.request().postDataJSON() as Json);
    return r.abort("connectionreset"); // the server made it, but the response is lost
  });
  // The server's record: the made session is unstarted (two-phase) — its view offers Start.
  await page.route(/\/api\/structured\/sessions\/codex-api(?::|%3A)[0-9a-f-]+(\/events)?(\?.*)?$/, (r) => {
    const made = bodies.length > 0 && r.request().url().includes(String(bodies[0].operation_id));
    if (!made) return r.fulfill({ status: 404, json: { detail: "no such native session" } });
    const s = snapshot({
      session_key: `codex-api:${bodies[0].operation_id}`,
      bypass: true,
      pending_start: true,
      native: null,
    });
    return r.request().url().includes("/events")
      ? r.fulfill({ json: { session_key: s.session_key, revision: 4, next_cursor: 4, events: [] } })
      : r.fulfill({ json: s });
  });
  await page.goto("/");
  await page.getByRole("combobox", { name: "Agent" }).selectOption("codex-api");
  await page.getByRole("checkbox", { name: /skip permission prompts/i }).check();
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page.getByTestId("start-error")).toContainText("waits in your session list");
  expect(await page.evaluate(() => sessionStorage.getItem("battlelab.pendingStructuredCreate"))).toBeNull();
  await page.reload();
  await page.getByRole("combobox", { name: "Agent" }).selectOption("codex-api");
  await expect(page.getByRole("checkbox", { name: /skip permission prompts/i })).not.toBeChecked();
  await expect(page.getByTestId("api-recovered-create")).toHaveCount(0);
  // Opened from the list, it waits — nothing ran — with Start / Discard.
  await page.goto(`/s/codex-api/${String(bodies[0].operation_id)}`);
  await expect(page.getByTestId("structured-pending-start")).toContainText("Nothing has run");
  await noOverflow(page);
  expect(bodies).toHaveLength(1);
});

test("New session: a later skip create never erases a lost guarded create — reload still offers it (Hermes 5916)", async ({
  page,
}) => {
  await setup(page, "dark", ["claude", "codex-api"]);
  const bodies: Json[] = [];
  await page.route("**/api/structured/sessions", async (r) => {
    const body = r.request().postDataJSON() as Json;
    bodies.push(body);
    if (bodies.length === 1) return r.abort("connectionreset"); // guarded: made, response lost
    await r.fulfill({ status: 201, json: snapshot({ bypass: true, pending_start: true, native: null }) });
  });
  await page.route(/\/api\/structured\/sessions\/codex-api(?::|%3A)[0-9a-f-]+(\/[a-z]+)?(\?.*)?$/, (r) => {
    const url = r.request().url();
    if (url.endsWith("/start")) return r.fulfill({ json: snapshot({ bypass: true }) });
    if (url.includes("/events")) return r.fulfill({ json: { session_key: `codex-api:${ID}`, revision: 4, next_cursor: 4, events: [] } });
    return r.fulfill({ json: snapshot({ session_key: `codex-api:${String(bodies[0]?.operation_id)}` }) });
  });
  await page.goto("/");
  const agent = page.getByRole("combobox", { name: "Agent" });
  await agent.selectOption("codex-api");
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page.getByTestId("start-error")).toBeVisible();
  await page.getByRole("checkbox", { name: /skip permission prompts/i }).check();
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(new RegExp(`/s/codex-api/${ID}$`));
  const slot = await page.evaluate(() => sessionStorage.getItem("battlelab.pendingStructuredCreate"));
  expect(JSON.parse(slot!).id).toBe(bodies[0].operation_id);
  await page.goto("/");
  await expect(page.getByTestId("api-recovered-create")).toHaveCount(0); // ordinary Claude selected
  await page.getByRole("combobox", { name: "Agent" }).selectOption("codex-api");
  await expect(page.getByTestId("api-recovered-create")).toContainText("previous Codex — API session is saved");
});

test("New session: a skip tick never survives an agent change (Hermes 5913)", async ({ page }) => {
  await setup(page, "dark", ["claude", "codex-api"]);
  await page.goto("/");
  const agent = page.getByRole("combobox", { name: "Agent" });
  const skip = page.getByRole("checkbox", { name: /skip permission prompts/i });
  await agent.selectOption("codex-api");
  await skip.check();
  await agent.selectOption("claude");
  await agent.selectOption("codex-api");
  await expect(skip).not.toBeChecked();
  await expect(page.getByTestId("api-bypass-warning")).toHaveCount(0);
});

test("New session: an API client whose adapter cannot skip prompts offers no toggle (#1339)", async ({
  page,
}) => {
  await setup(page, "dark", ["claude", "codex-api"]);
  await mockRoster(page, {
    overrides: {
      "codex-api": {
        present: true,
        supports_new: true,
        api: { kind: "codex-app-server", source: "codex", unavailable_reason: null, can_bypass: false },
      },
    },
  });
  await page.goto("/");
  await page.getByRole("combobox", { name: "Agent" }).selectOption("codex-api");
  await expect(page.getByRole("checkbox", { name: /skip permission prompts/i })).toHaveCount(0);
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
    // The trigger never overlaps the facts run beside it (the terminal's own bar, #1348).
    const t = (await pane.getByTestId("head-actions-menu").boundingBox())!;
    const led = (await pane.locator("[data-panel-head] [data-head-led]").boundingBox())!;
    expect(t.x).toBeGreaterThanOrEqual(led.x + led.width);
    await pane.getByTestId("head-actions-menu").click();
    await expect(page.getByRole("menuitem")).toHaveText(["Files", "Recap", "Hand off", "Share link", "Stop"]);
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
  await expect(win.locator("[data-panel-head]")).toHaveCount(0);
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

for (const theme of ["dark", "light"] as const) {
  for (const bypass of [true, false]) {
    test(`in a map window a skip session still says SKIP PERMISSIONS — bypass=${bypass} (#1339, ${theme})`, async ({
      page,
    }, testInfo) => {
      test.skip(testInfo.project.name !== "desktop", "windows are desktop-only (#208)");
      await setup(page, theme);
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
            theme,
          },
        }),
      );
      await page.route(/\/api\/projects(\?.*)?$/, (r) =>
        r.fulfill({ json: { projects: [{ id: "p1", name: "proj", color: "#ffb000", archived: false }] } }),
      );
      await serveSession(page, () =>
        snapshot({ bypass, turns: [turn({ state: "completed", tools: [], reply: "done" })] }),
      );
      await page.goto("/overview");
      await page.locator(".tr-ov-chip").first().click();
      const win = page.locator(`[data-session-window="codex-api:${ID}"]`);
      await expect(win.getByTestId("structured-turn")).toBeVisible();
      await expect(win.locator("[data-panel-head]")).toHaveCount(0); // the pane's head is hidden here
      const banner = win.getByTestId("structured-bypass-banner");
      if (bypass) {
        await expect(banner).toBeVisible();
        await expect(banner).toContainText(/skip permissions/i);
        const colors = await banner.evaluate((el) => {
          const actual = getComputedStyle(el);
          const probe = document.createElement("span");
          probe.style.borderLeftColor = "var(--status-degraded)";
          probe.style.color = "var(--warn-text)";
          el.append(probe);
          const expected = getComputedStyle(probe);
          const colors = {
            actual: { border: actual.borderLeftColor, text: actual.color },
            expected: { border: expected.borderLeftColor, text: expected.color },
          };
          probe.remove();
          return colors;
        });
        expect(colors.actual).toEqual(colors.expected);
      } else {
        await expect(banner).toHaveCount(0);
      }
    });
  }
}

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
    // The terminal composer's pill (#1348): the name, whole, and its ×.
    expect(await chip.getByText("shot.png").evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(0);
    await expect(chip.getByRole("button", { name: "Remove shot.png" })).toBeVisible();
    await noOverflow(page);
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
    const tools = page.getByRole("form", { name: "Compose message" });
    await expect(tools).toBeVisible();
    await terminalChips(tools);
    await noOverflow(page);
    await tools.getByRole("button", { name: "Use a template" }).click();
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
    await tools.getByRole("button", { name: "Sent messages" }).click();
    await page.getByRole("button", { name: /restore/i }).first().click();
    await expect(box).toHaveValue("Reproduce the logout bug in a real browser");
    await expect(page.getByTestId("structured-attachment")).toHaveCount(1);
    await noOverflow(page);
  });
}

// ---- push-to-talk (#1332 Phase 3c) -------------------------------------------------------------

const API_SPEECH_STUB = `
window.__recog = { started: 0, stopped: 0 };
window.SpeechRecognition = class {
  constructor() { this.onresult = null; this.onerror = null; this.onend = null; }
  start() {
    window.__recog.started++;
    setTimeout(() => this.onresult && this.onresult({ resultIndex: 0,
      results: [{ 0: { transcript: "check the logout" }, isFinal: false, length: 1 }] }), 30);
    setTimeout(() => this.onresult && this.onresult({ resultIndex: 0,
      results: [{ 0: { transcript: "check the logout spec" }, isFinal: true, length: 1 }] }), 80);
  }
  stop() { window.__recog.stopped++; setTimeout(() => this.onend && this.onend(), 20); }
  abort() { this.onend && this.onend(); }
};
Object.defineProperty(navigator, "mediaDevices", {
  configurable: true,
  value: { getUserMedia: () => Promise.resolve({ getTracks: () => [{ stop() {} }] }) },
});
`;

for (const theme of ["dark", "light"] as const) {
  test(`hold to talk dictates into the API message and it sends (#1332, ${theme})`, async ({ page }) => {
    await page.addInitScript(API_SPEECH_STUB);
    await setup(page, theme);
    let posted: Json | null = null;
    await serveSession(page, () => snapshot());
    await page.route(new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}/turns$`), async (r) => {
      posted = r.request().postDataJSON() as Json;
      await r.fulfill({ status: 202, json: { state: "running" } });
    });
    await page.goto(URL_PATH);
    const tools = page.getByRole("form", { name: "Compose message" });
    const mic = tools.getByRole("button", { name: /voice input/i });
    await expect(mic).toBeVisible();
    await terminalChips(tools);
    await noOverflow(page);
    const box = (await mic.boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    const text = page.getByRole("textbox", { name: /Message/ });
    await expect(text).toHaveValue("check the logout spec");
    await expect(mic).toHaveAttribute("aria-pressed", "true");
    await page.mouse.up();
    await expect(mic).toHaveAttribute("aria-label", "Start voice input — hold to talk");
    await page.getByRole("button", { name: "Send" }).click();
    await expect.poll(() => posted).not.toBeNull();
    expect(posted).toMatchObject({ text: "check the logout spec" });
    // Space in the page never dictates here: the terminal composer owns that hotkey.
    await page.locator("body").press("Space");
    expect(await page.evaluate(() => (window as unknown as { __recog: { started: number } }).__recog.started)).toBe(1);
  });
}

// ---- #1339: approve always + risk marks -----------------------------------------------------------

const ALWAYS_PERSIST = {
  id: "g0123456789abcdef",
  scope: "persistent",
  label:
    "Always allow `cat shared/agent-workflow.md` and any command starting with it · saved to Codex's exec policy (persists)",
};
const ALWAYS_SESSION = {
  id: "gfedcba9876543210",
  scope: "session",
  label: "Allow `cat shared/agent-workflow.md` for the rest of this session",
};

function askState(over: Json): Json {
  return snapshot({
    state: "awaiting_approval",
    active_turn: TURN,
    turns: [turn()],
    pending_requests: [
      {
        request_id: "7",
        turn_id: TURN,
        kind: "command",
        choices: ["approve", "reject", "cancel"],
        complete: true,
        payload_digest: "3b0e9d2aa41c",
        payload: { command: "/bin/bash -lc 'cat shared/agent-workflow.md'", cwd: "/home/u/proj" },
        ...over,
      },
    ],
  });
}

for (const theme of ["dark", "light"] as const) {
  test(`Approve always offers the request's own grants with their breadth and sends one (${theme}) (#1339)`, async ({
    page,
  }) => {
    await setup(page, theme);
    let decided: Json | null = null;
    let state = askState({ always: [ALWAYS_PERSIST, ALWAYS_SESSION], risk: { level: "none", reasons: [] } });
    await serveSession(page, () => state, {
      decide: async (r, body) => {
        decided = body;
        state = snapshot({ revision: 6, event_cursor: 6, turns: [turn({ state: "completed", reply: "done" })] });
        await r.fulfill({ json: { decision: "always" } });
      },
    });
    await page.goto(URL_PATH);
    const card = page.getByTestId("structured-request");
    await expect(card).toBeVisible();
    await expect(card.getByTestId("structured-risk")).toHaveCount(0);
    const toggle = card.getByTestId("structured-always");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    const options = card.getByTestId("structured-always-option");
    await expect(options).toHaveText([ALWAYS_PERSIST.label, ALWAYS_SESSION.label]);
    await expect(card).toContainText("BattleLab never adds or widens one");
    await noOverflow(page);
    await targets(page, card);
    // Opening moves focus into the grants; Escape closes them and returns it to the toggle.
    await expect(options.first()).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(options).toHaveCount(0);
    await expect(toggle).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(options.first()).toBeFocused();
    await options.first().click();
    await expect(page.getByTestId("structured-request")).toHaveCount(0);
    expect(decided).toMatchObject({ request_id: "7", turn_id: TURN, decision: "always", grant: ALWAYS_PERSIST.id });
  });

  test(`a risky command offers a labelled standing grant and sends the chosen id (${theme}) (#1339)`, async ({
    page,
  }) => {
    await setup(page, theme);
    const risky = { level: "risky", reasons: ["force-pushes over remote history"] };
    let decided: Json | null = null;
    const grant = {
      ...ALWAYS_PERSIST,
      label: "Always allow `git push --force` and any command starting with it · saved to Codex's exec policy (persists) · RISKY: force-pushes over remote history",
    };
    let state = askState({
      payload: { command: "git push --force origin main", cwd: "/home/u/proj" },
      always: [grant],
      risk: risky,
    });
    (state.turns as Json[])[0] = turn({
      tools: [
        { id: "t1", name: "commandExecution", outcome: "completed", summary: "rm -rf dist", risk: { level: "risky", reasons: ["deletes files recursively or forcibly"] } },
        { id: "t2", name: "commandExecution", outcome: "completed", summary: "‹unreadable›", risk: { level: "unknown", reasons: [] } },
      ],
    });
    await serveSession(page, () => state, {
      decide: async (r, body) => {
        decided = body;
        state = snapshot({ revision: 6, event_cursor: 6, turns: [turn({ state: "completed", reply: "done" })] });
        await r.fulfill({ json: { decision: "always" } });
      },
    });
    await page.goto(URL_PATH);
    const card = page.getByTestId("structured-request");
    await expect(card.getByTestId("structured-risk")).toContainText("force-pushes over remote history");
    await card.getByTestId("structured-always").click();
    const option = card.getByTestId("structured-always-option");
    await expect(option).toHaveText(grant.label);
    const rows = page.getByTestId("structured-tool");
    await expect(rows.nth(0).getByTestId("structured-risk")).toContainText("deletes files recursively");
    await expect(rows.nth(1).getByTestId("structured-risk")).toHaveText("not classified");
    // The mark is the WARNING status colour, never the failure red.
    const color = await card.getByTestId("structured-risk").evaluate((el) => getComputedStyle(el).borderLeftColor);
    const degraded = await page.evaluate(() => {
      const probe = document.createElement("span");
      probe.style.color = "var(--status-degraded)";
      document.body.append(probe);
      const c = getComputedStyle(probe).color;
      probe.remove();
      return c;
    });
    expect(color).toBe(degraded);
    await noOverflow(page);
    await targets(page, card);
    await option.click();
    await expect(page.getByTestId("structured-request")).toHaveCount(0);
    expect(decided).toMatchObject({ request_id: "7", turn_id: TURN, decision: "always", grant: grant.id });
  });
}

// Shared responses are pinned to native_grants.derive by test_api_always.py.
for (const theme of ["dark", "light"] as const) {
  for (const fixture of directoryGrants) {
    test(`a directory grant with ${fixture.risk.level} classification discloses its risk at consent (${theme}) (#1339)`, async ({ page }) => {
      await setup(page, theme);
      let decided: Json | null = null;
      let state = askState({ kind: "Bash", ...fixture });
      await serveSession(page, () => state, {
        decide: async (r, body) => {
          decided = body;
          state = snapshot({ revision: 6, event_cursor: 6, turns: [turn({ state: "completed", reply: "done" })] });
          await r.fulfill({ json: { decision: "always" } });
        },
      });
      await page.goto(URL_PATH);
      const card = page.getByTestId("structured-request");
      const note = fixture.risk.level === "risky"
        ? "RISKY: deletes files recursively or forcibly"
        : "not classified";
      const mark = card.getByTestId("structured-risk");
      await expect(mark).toBeVisible();
      await card.getByTestId("structured-always").click();
      const option = card.getByTestId("structured-always-option");
      await expect(option).toContainText("Allow access to `/srv/data` · this session only");
      await expect(option).toContainText(note);
      await expect(mark).toBeVisible();
      await targets(page, card);
      await noOverflow(page);
      await option.click();
      await expect(page.getByTestId("structured-request")).toHaveCount(0);
      expect(decided).toMatchObject({ request_id: "7", turn_id: TURN, decision: "always", grant: fixture.always[0].id });
    });
  }
}

test("API transcript follows streaming growth only while at the bottom (#1379)", async ({ page }) => {
  await setup(page, "dark");
  const paragraphs = (count: number) => Array.from({ length: count }, (_, i) => `Progress paragraph ${i + 1}.`).join("\n\n");
  let revision = 4;
  let reply = paragraphs(90);
  const turns = () => [turn({ state: "running", tools: [], reply })];
  let extra: Json[] = [];
  await serveSession(page, () => snapshot({ state: "running", active_turn: TURN, revision,
    event_cursor: revision, turns: [...turns(), ...extra] }));
  await page.goto(URL_PATH);
  await expect(page.getByText("Progress paragraph 90.", { exact: true })).toBeVisible();
  const log = page.getByTestId("structured-turn").first().locator("..");
  const gap = () => log.evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop);
  await expect.poll(gap).toBeLessThanOrEqual(3);
  reply = paragraphs(110); revision++;
  await expect(page.getByText("Progress paragraph 110.", { exact: true })).toBeAttached();
  await expect.poll(gap).toBeLessThanOrEqual(3);
  await log.evaluate((el) => { el.scrollTop -= 400; });
  await expect.poll(gap).toBeGreaterThan(390);
  const reading = await log.evaluate((el) => el.scrollTop);
  reply = paragraphs(120); revision++;
  extra = [turn({ turn_id: "22222222-2222-4333-8444-555555555555", state: "queued", text: "Follow-up", reply: "", tools: [] })];
  await expect(page.getByText("Follow-up", { exact: true })).toBeAttached();
  await expect.poll(() => log.evaluate((el) => el.scrollTop)).toBe(reading);
  await log.evaluate((el) => { el.scrollTop = el.scrollHeight; });
  await expect.poll(gap).toBeLessThanOrEqual(3);
  reply = paragraphs(135); revision++;
  await expect(page.getByText("Progress paragraph 135.", { exact: true })).toBeAttached();
  await expect.poll(gap).toBeLessThanOrEqual(3);
  // A slow upward drag can arrive as separate one-pixel scroll events.
  await log.evaluate(async (el) => {
    for (let i = 0; i < 6; i++) {
      el.scrollTop -= 1;
      await new Promise(requestAnimationFrame);
      await new Promise(requestAnimationFrame);
    }
  });
  await expect.poll(gap).toBeGreaterThan(5);
  const slowReading = await log.evaluate((el) => el.scrollTop);
  reply = paragraphs(136); revision++;
  await expect(page.getByText("Progress paragraph 136.", { exact: true })).toBeAttached();
  await expect.poll(() => log.evaluate((el) => el.scrollTop)).toBe(slowReading);
  await log.evaluate((el) => new Promise<void>((resolve) => {
    el.addEventListener("scroll", () => resolve(), { once: true });
    el.scrollTop = el.scrollHeight;
  }));
  await expect.poll(gap).toBeLessThanOrEqual(3);
  const viewport = page.viewportSize()!;
  await page.setViewportSize({ width: viewport.width, height: viewport.height - 100 });
  await expect.poll(gap).toBeLessThanOrEqual(3);
  // Late layout growth (like a decoded image) changes height without a text/count mutation.
  await page.getByTestId("structured-turn").first().evaluate((el) => { el.style.paddingBottom = "240px"; });
  await expect.poll(gap).toBeLessThanOrEqual(3);
  await log.evaluate((el) => { el.scrollTop -= 300; });
  await expect.poll(gap).toBeGreaterThan(290);
  const beforeResize = await log.evaluate((el) => el.scrollTop);
  await page.getByTestId("structured-turn").first().evaluate((el) => { el.style.paddingBottom = "480px"; });
  await expect.poll(() => log.evaluate((el) => el.scrollTop)).toBe(beforeResize);
  // A new session starts pinned even when this one was left reading older content.
  const nextId = "6b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
  await page.route(new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${nextId}`), (r) =>
    r.fulfill({ json: snapshot({ session_key: `codex-api:${nextId}`, state: "running", active_turn: TURN,
      turns: [turn({ state: "running", reply: paragraphs(100), tools: [] })] }) }),
  );
  await page.evaluate((path) => {
    history.pushState({}, "", path);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, `/s/codex-api/${nextId}`);
  await expect(page.getByText("Progress paragraph 100.", { exact: true })).toBeAttached();
  await expect.poll(gap).toBeLessThanOrEqual(3);
});
