import { expect, test, type Page } from "@playwright/test";
import { mockRoster } from "./roster";

// #1209 (#853 P9a-3): an API agent's session is a CHAT PANE, not a terminal. The server is mocked:
// `GET /api/chat/<sid>` is a small state machine, so the spec drives the real client through a
// send → pending → reply cycle, a reload mid-request, a failed turn with Retry, and the
// unconfigured state — on desktop and mobile, in both themes.

const SID = "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b";
const KEY = `apichat:${SID}`;
const URL_PATH = `/s/apichat/${SID}`;

type Turn = Record<string, unknown>;

function turn(over: Turn): Turn {
  const now = Date.now() / 1000;
  return {
    turn_id: "11111111-2222-4333-8444-555555555555",
    text: "hello",
    ts: now,
    status: "done",
    reason: null,
    reply: "hi",
    reply_ts: now,
    usage: { total_tokens: 1234 },
    truncated: false,
    dropped: 0,
    ...over,
  };
}

async function setup(
  page: Page,
  theme: "dark" | "light",
  opts: { configured?: boolean } = {},
) {
  const configured = opts.configured ?? true;
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.route("**/api/**", (r) =>
    r.fulfill({ status: 404, json: { detail: "not mocked" } }),
  );
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: configured ? ["apichat"] : [],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
        theme,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route(`**/api/agents/apichat/endpoint`, (r) =>
    r.fulfill({
      json: {
        base_url: configured ? "https://llm.example.lan/v1" : "",
        model: configured ? "qwen3-coder" : "",
        api_key_set: configured,
        context_window: 32768,
        max_output_tokens: 4096,
        request_timeout: null,
        configured,
      },
    }),
  );
  await mockRoster(page, {
    overrides: { apichat: { present: configured, supports_new: configured } },
  });
}

async function noOverflow(page: Page) {
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

for (const theme of ["dark", "light"] as const) {
  test(`a conversation: send, wait, reply — and a reload mid-request recovers it (${theme})`, async ({
    page,
  }) => {
    await setup(page, theme);
    let turns: Turn[] = [];
    let reads = 0;
    const sent: Array<{ turn_id: string; text: string }> = [];
    await page.route(
      `**/api/chat/${encodeURIComponent(KEY)}/messages`,
      async (r) => {
        const body = r.request().postDataJSON() as {
          turn_id: string;
          text: string;
        };
        sent.push(body);
        turns = [
          turn({
            turn_id: body.turn_id,
            text: body.text,
            status: "pending",
            reply: null,
            usage: null,
          }),
        ];
        reads = 0;
        await r.fulfill({
          status: 202,
          json: { turn: { turn_id: body.turn_id, status: "pending" } },
        });
      },
    );
    await page.route(`**/api/chat/${encodeURIComponent(KEY)}`, (r) => {
      reads += 1;
      if (turns[0]?.status === "pending" && reads >= 3) {
        turns = [
          {
            ...turns[0],
            status: "done",
            reply: "It walks an ordered list of degrades.",
            reply_ts: Date.now() / 1000,
          },
        ];
      }
      const pending = turns.find((t) => t.status === "pending");
      return r.fulfill({
        json: {
          session_id: SID,
          cwd: "/w",
          created_at: 0,
          turns,
          in_flight: pending ? pending.turn_id : null,
        },
      });
    });

    await page.goto(URL_PATH);
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    const pane = page.getByTestId("chat-pane");
    await expect(pane.getByTestId("chat-empty")).toContainText(
      "it can only reply",
    );
    await expect(page.locator(".xterm")).toHaveCount(0); // a chat agent gets no terminal

    const box = page.getByLabel("Message the agent");
    await box.fill("Why does _post_chat retry twice?");
    await box.press("Enter");
    await expect(pane.getByTestId("chat-waiting")).toBeVisible();
    expect(sent).toHaveLength(1);
    expect(sent[0].turn_id).toMatch(/^[0-9a-f-]{36}$/);

    // The operator reloads while the reply is pending: the page reads, it never resends.
    await page.reload();
    await expect(
      page.getByText("It walks an ordered list of degrades."),
    ).toBeVisible();
    expect(sent).toHaveLength(1);

    const send = page.getByRole("button", { name: "Send" });
    expect((await send.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    await noOverflow(page);
  });
}

test("a failed turn says why, keeps the message, and Retry resends that same turn", async ({
  page,
}) => {
  await setup(page, "dark");
  const failed = turn({
    status: "failed",
    reply: null,
    usage: null,
    dropped: 2,
    reason:
      "the endpoint did not answer within 120s — it may already have processed (and billed) this request; Retry sends it again",
  });
  const retried: string[] = [];
  await page.route(`**/api/chat/${encodeURIComponent(KEY)}`, (r) =>
    r.fulfill({
      json: {
        session_id: SID,
        cwd: "/w",
        created_at: 0,
        turns: [failed],
        in_flight: null,
      },
    }),
  );
  await page.route(
    `**/api/chat/${encodeURIComponent(KEY)}/turns/*/retry`,
    async (r) => {
      retried.push(r.request().url());
      await r.fulfill({
        status: 202,
        json: { turn: { turn_id: failed.turn_id, status: "pending" } },
      });
    },
  );
  await page.goto(URL_PATH);
  await expect(page.getByTestId("chat-failed")).toContainText(
    "may already have processed",
  );
  await expect(page.getByTestId("chat-failed")).toContainText(
    "Your message was kept",
  );
  await expect(page.getByTestId("chat-dropped")).toContainText(
    "2 earlier turns were not sent",
  );
  const retry = page.getByRole("button", { name: "Retry" });
  expect((await retry.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  await retry.click();
  await expect.poll(() => retried.length).toBe(1);
  expect(retried[0]).toContain(`/turns/${failed.turn_id}/retry`);
  await noOverflow(page);
});

test("an unconfigured agent sends nothing and links to its settings", async ({
  page,
}) => {
  await setup(page, "light", { configured: false });
  await page.route(`**/api/chat/${encodeURIComponent(KEY)}`, (r) =>
    r.fulfill({
      json: {
        session_id: SID,
        cwd: "/w",
        created_at: 0,
        turns: [],
        in_flight: null,
      },
    }),
  );
  await page.goto(URL_PATH);
  await expect(page.getByTestId("chat-unconfigured")).toContainText(
    "Nothing is sent",
  );
  const link = page.getByRole("link", { name: /open agent settings/i });
  await expect(link).toHaveAttribute("href", "/settings/agents/apichat");
  expect((await link.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  await noOverflow(page);
});

test("Start on an API agent creates the conversation on the server and opens it — no bypass box", async ({
  page,
}) => {
  await setup(page, "dark");
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["apichat"],
        terminal_backend: "ws",
        auth_mode: "none",
        default_project: "/home/u/proj",
      },
    }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  const created: unknown[] = [];
  await page.route("**/api/chat/new", async (r) => {
    created.push(r.request().postDataJSON());
    await r.fulfill({ status: 201, json: { id: KEY } });
  });
  await page.route(`**/api/chat/${encodeURIComponent(KEY)}`, (r) =>
    r.fulfill({
      json: {
        session_id: SID,
        cwd: "/home/u/proj",
        created_at: 0,
        turns: [],
        in_flight: null,
      },
    }),
  );
  await page.goto("/");
  await expect(page.getByLabel("Launch folder")).toHaveValue("/home/u/proj");
  await expect(page.getByText("Skip permission prompts")).toHaveCount(0);
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(new RegExp(`/s/apichat/${SID}$`));
  expect(created).toEqual([{ engine: "apichat", cwd: "/home/u/proj" }]);
  await expect(page.getByTestId("chat-empty")).toBeVisible();
});

test("the Endpoint card: TEST never saves, SAVE activates the agent", async ({
  page,
}) => {
  await setup(page, "dark", { configured: false });
  const saved: unknown[] = [];
  const tested: unknown[] = [];
  await page.route("**/api/engines/apichat", (r) =>
    r.fulfill({
      json: {
        id: "apichat",
        label: "API agent",
        publisher: "battlelab",
        version: "1",
        contract: 1,
        source: "in-tree",
        kind: "agent",
        runtime: "chat",
        status: "active",
        binary: null,
        endpoint: { kind: "openai-chat" },
        provenance: { state: "absent", via: null, path: null, note: null },
        store: {
          root: "~/.local/share/agent-sessions/chat",
          resolved: null,
          layout: "battlelab-chat",
          read_only: false,
        },
        launch: null,
        transcript: { kind: "battlelab-chat", strict: false },
        usage: { source: "tokens", kind: "chat-response-tokens" },
        capabilities: {
          resume: true,
          new: true,
          archive: true,
          handoff_target: false,
          seed_start: false,
          orchestrator_input: false,
          raw_tty: false,
          owns_transcript: false,
        },
        models: [],
        display: {
          name: "API agent",
          badge: "api",
          accent: "blue",
          id_prefix: null,
          order: 80,
        },
      },
    }),
  );
  await page.route("**/api/agents/apichat/endpoint/test", async (r) => {
    tested.push(r.request().postDataJSON());
    await r.fulfill({ json: { models: ["qwen3-coder"], listing: "ok" } });
  });
  await page.route("**/api/agents/apichat/endpoint", async (r) => {
    if (r.request().method() === "PATCH") {
      saved.push(r.request().postDataJSON());
      return r.fulfill({
        json: {
          base_url: "https://llm.example.lan/v1",
          model: "qwen3-coder",
          api_key_set: true,
          context_window: 32768,
          max_output_tokens: 4096,
          request_timeout: null,
          configured: true,
        },
      });
    }
    return r.fulfill({
      json: {
        base_url: "",
        model: "",
        api_key_set: false,
        context_window: 32768,
        max_output_tokens: 4096,
        request_timeout: null,
        configured: false,
      },
    });
  });
  await page.goto("/settings/agents/apichat");
  const card = page.getByRole("form", { name: "Endpoint" });
  await card.getByLabel("Base URL").fill("https://llm.example.lan/v1");
  await card.getByLabel("API key").fill("sk-test-123");
  await card.getByRole("button", { name: "Test" }).click();
  await expect(page.getByTestId("endpoint-status")).toContainText(
    "Nothing was saved",
  );
  expect(saved).toEqual([]);
  expect(tested).toEqual([
    { base_url: "https://llm.example.lan/v1", api_key: "sk-test-123" },
  ]);
  await card.getByLabel("Model").fill("qwen3-coder");
  await card.getByRole("button", { name: "Save" }).click();
  await expect(page.getByTestId("endpoint-status")).toContainText(
    "available now",
  );
  expect(saved).toEqual([
    {
      base_url: "https://llm.example.lan/v1",
      model: "qwen3-coder",
      api_key: "sk-test-123",
    },
  ]);
  await expect(card.getByLabel("API key")).toHaveValue("");
  for (const name of ["Test", "Save"]) {
    expect(
      (await card.getByRole("button", { name }).boundingBox())!.height,
    ).toBeGreaterThanOrEqual(44);
  }
  await noOverflow(page);
});
