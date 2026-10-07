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
