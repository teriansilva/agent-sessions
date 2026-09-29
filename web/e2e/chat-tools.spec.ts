import { expect, test, type Page } from "@playwright/test";
import { mockRoster } from "./roster";

// #1222 (#853 P9b): an API agent with READ TOOLS — its tool rows and its Tools setting. Built on the
// #1209 harness: an API agent's session is a CHAT PANE, not a terminal. The server is mocked:
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
  opts: { configured?: boolean; tools?: "none" | "read" } = {},
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
        tools: opts.tools ?? "none",
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


const now = Date.now() / 1000;
const LONG = `src/${"deeply/nested/".repeat(10)}module_with_a_rather_long_name.py`;
const TOOLS = [
  { call_id: "c1", name: "list_files", path: "src", outcome: "ok", entries: 42 },
  { call_id: "c2", name: "read_file", path: LONG, outcome: "ok", start_line: 1, end_line: 200, total_lines: 350 },
  { call_id: "c3", name: "read_file", path: ".env", outcome: "refused", reason: "hidden path — never readable by the agent" },
];

async function conversation(page: Page, turns: Turn[], inFlight: string | null = null) {
  await page.route(`**/api/chat/${encodeURIComponent(KEY)}`, (r) =>
    r.fulfill({
      json: { session_id: SID, cwd: "/home/op/proj", created_at: now - 60, turns, in_flight: inFlight },
    }),
  );
}

for (const theme of ["dark", "light"] as const) {
  test(`tool rows: listed, read, refused and running — long paths wrap, nothing overflows (${theme})`, async ({
    page,
  }) => {
    await setup(page, theme, { tools: "read" });
    await conversation(
      page,
      [
        turn({ turn_id: "t1", text: "where is retry?", reply: "In `_begin`.", tools: TOOLS }),
        turn({
          turn_id: "t2",
          text: "and the budget?",
          status: "pending",
          reply: null,
          usage: null,
          tools: [{ call_id: "d1", name: "read_file", path: "src/chat_config.py", outcome: "running" }],
        }),
      ],
      "t2",
    );
    await page.goto(URL_PATH);
    const rows = page.getByTestId("chat-tool");
    await expect(rows).toHaveCount(4);
    await expect(rows.nth(0)).toContainText("Listed");
    await expect(rows.nth(1)).toContainText(LONG);
    await expect(rows.nth(2)).toHaveAttribute("data-outcome", "refused");
    await expect(page.getByTestId("chat-waiting")).toContainText("reading src/chat_config.py");
    // The long path wraps inside the pane instead of widening it.
    const log = page.getByTestId("chat-pane");
    const [rowBox, paneBox] = await Promise.all([rows.nth(1).boundingBox(), log.boundingBox()]);
    expect(rowBox!.x + rowBox!.width).toBeLessThanOrEqual(paneBox!.x + paneBox!.width + 0.5);
    await noOverflow(page);
  });
}

for (const tools of ["none", "read"] as const) {
  test(`the empty conversation says which mode is on (${tools})`, async ({ page }) => {
    await setup(page, "dark", { tools });
    await conversation(page, []);
    await page.goto(URL_PATH);
    await expect(page.getByTestId("chat-empty")).toContainText(
      tools === "read" ? "list and read files" : "It has no tools",
    );
  });
}

test("Tools: chosen by keyboard, 44 px targets, Save reachable and sends only tools", async ({
  page,
}) => {
  await setup(page, "light", { tools: "none" });
  const saved: unknown[] = [];
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
  await page.route("**/api/agents/apichat/endpoint", async (r) => {
    const base = {
      base_url: "https://llm.example.lan/v1",
      model: "qwen3-coder",
      api_key_set: true,
      context_window: 32768,
      max_output_tokens: 4096,
      request_timeout: null,
      configured: true,
      tools: "none",
    };
    if (r.request().method() === "PATCH") {
      saved.push(r.request().postDataJSON());
      return r.fulfill({ json: { ...base, tools: "read" } });
    }
    return r.fulfill({ json: base });
  });
  await page.goto("/settings/agents/apichat");
  const card = page.getByRole("form", { name: "Endpoint" });
  const none = card.getByRole("radio", { name: /None/ });
  const read = card.getByRole("radio", { name: /Read files/ });
  await expect(none).toBeChecked();
  for (const opt of [none, read]) {
    const box = await opt.locator("xpath=ancestor::label[1]").boundingBox();
    expect(box!.height).toBeGreaterThanOrEqual(44);
  }
  await none.focus();
  await page.keyboard.press("ArrowDown");
  await expect(read).toBeChecked();
  const save = card.getByRole("button", { name: "Save" });
  await save.scrollIntoViewIfNeeded();
  await expect(save).toBeInViewport();
  await save.click();
  await expect(page.getByTestId("endpoint-status")).toContainText("available now");
  expect(saved).toEqual([{ tools: "read" }]);
  await noOverflow(page);
});
