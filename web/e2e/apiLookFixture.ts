// Two API panes with representative content (#1494 P3) — a codex-api StructuredPane and an
// apichat ChatPane — each with a user turn, a reply carrying Markdown (bold, inline code, a
// fenced block), and a command row. Used by the default-look baseline and the API-look specs, so
// both measure the same DOM. `long` adds the long-content state: a wide unbroken code line and a
// long command, for the overflow checks. Network fully mocked.
import { expect, type Page } from "@playwright/test";
import { mockRoster } from "./roster";

export const STRUCTURED_ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
export const STRUCTURED_PATH = `/s/codex-api/${STRUCTURED_ID}`;
export const CHAT_SID = "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b";
export const CHAT_PATH = `/s/apichat/${CHAT_SID}`;

const SESSION = new RegExp(
  `/api/structured/sessions/codex-api(?::|%3A)${STRUCTURED_ID}(/[a-z]+)?(\\?.*)?$`,
);
const WIDE = "const veryLongIdentifierThatNeverBreaks_" + "x".repeat(220) + " = 1;";
const LONG_CMD =
  "npx playwright test e2e/login.spec.ts " + "--grep=sign-in-redirect-under-load ".repeat(8);

// Every Markdown construct the pane styles (Markdown.module.css), so the baseline measures each rule:
// headings, emphasis, inline code, lists, a link, a quote, a rule, a table and a fenced block.
const reply = (long: boolean) =>
  "# Diagnosis\n\n## Light mode\n\n" +
  "The **diff tokens** mix the status hue at 8% on light, _too faint_ on `--surface-3`.\n\n" +
  "- one square bullet\n- [the design doc](https://example.com/design)\n\n1. first\n2. second\n\n" +
  "> A quoted note.\n\n---\n\n| Token | Light |\n|---|---|\n| `--diff-add-bg` | 8% |\n\n" +
  "```css\n--diff-add-bg: color-mix(in srgb, var(--status-up) 14%, transparent);\n" +
  (long ? WIDE + "\n" : "") +
  "```\n\nRun `npm test` to confirm.";

// A fixed instant, so nothing in the measured DOM depends on the clock.
const TS = 1_790_000_000;

export async function mockApiPanes(
  page: Page,
  opts: { theme?: "dark" | "light"; long?: boolean } = {},
): Promise<void> {
  const long = opts.long ?? false;
  if (opts.theme) await page.addInitScript((t) => localStorage.setItem("tr-theme", t), opts.theme);
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["codex-api", "apichat"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
        agent_defaults: { bypass: false },
      },
    }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
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
      apichat: { present: true, supports_new: true },
    },
  });

  const snapshot = {
    session_key: `codex-api:${STRUCTURED_ID}`,
    revision: 4,
    event_cursor: 4,
    cwd: "/home/u/proj",
    state: "idle",
    active_turn: null,
    model_requested: null,
    model_effective: "gpt-5-codex",
    turns: [
      {
        turn_id: "11111111-2222-4333-8444-555555555555",
        operation_id: "11111111-2222-4333-8444-555555555555",
        state: "completed",
        text: "Why does the light-mode diff look washed out?",
        reply: reply(long),
        text_truncated: false,
        reply_truncated: false,
        reason: null,
        tools: [
          { id: "c1", name: "command", outcome: "ok", summary: long ? LONG_CMD : "rg --files web/src/theme" },
        ],
        tools_truncated: false,
        created_at: TS,
        completed_at: TS + 30,
      },
    ],
    omitted_turns: 0,
    pending_requests: [],
    native: { native_id: "n1", worker: "w1", background_active: false },
    read_only: null,
  };
  await page.route(SESSION, async (r) => {
    const sub = r.request().url().match(SESSION)?.[1] ?? "";
    if (sub === "/events")
      return r.fulfill({
        json: { session_key: snapshot.session_key, revision: 4, next_cursor: 4, events: [] },
      });
    if (sub === "") return r.fulfill({ json: snapshot });
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
  });

  await page.route(`**/api/agents/apichat/endpoint`, (r) =>
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
  await page.route(`**/api/chat/${encodeURIComponent(`apichat:${CHAT_SID}`)}`, (r) =>
    r.fulfill({
      json: {
        session_id: CHAT_SID,
        cwd: "/w",
        created_at: 0,
        in_flight: null,
        turns: [
          {
            turn_id: "11111111-2222-4333-8444-555555555555",
            text: "Why does _post_chat retry twice?",
            ts: TS,
            status: "done",
            reason: null,
            reply: reply(long),
            reply_ts: TS + 5,
            usage: { total_tokens: 1234 },
            truncated: false,
            dropped: 0,
          },
        ],
      },
    }),
  );
}

export async function openStructured(page: Page): Promise<void> {
  await page.goto(STRUCTURED_PATH);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await expect(page.getByTestId("md-code").first()).toBeVisible();
}

export async function openChat(page: Page): Promise<void> {
  await page.goto(CHAT_PATH);
  await expect(page.getByTestId("chat-pane")).toBeVisible();
  await expect(page.getByTestId("chat-turn").first()).toBeVisible();
}

/** Every element under `root` (itself included), keyed by its DOM path, with the computed styles
 *  a look can change. Layout geometry is left out on purpose: it depends on the viewport, not on
 *  the look. The cursor is parked so no `:hover` rule is in force. */
export async function computedLook(page: Page, testId: string): Promise<Record<string, Record<string, string>>> {
  await page.mouse.move(0, 0);
  return page.getByTestId(testId).evaluate((root) => {
    const PROPS = [
      "color", "background-color", "background-image", "font-family", "font-size", "font-weight",
      "font-style", "line-height", "letter-spacing", "text-transform",
      "border-top-color", "border-right-color", "border-bottom-color", "border-left-color",
      "border-top-width", "border-right-width", "border-bottom-width", "border-left-width",
      "padding-top", "padding-right", "padding-bottom", "padding-left",
      "margin-top", "margin-bottom", "row-gap", "column-gap",
    ];
    const out: Record<string, Record<string, string>> = {};
    const walk = (el: Element, path: string) => {
      const cs = getComputedStyle(el);
      out[path] = Object.fromEntries(PROPS.map((p) => [p, cs.getPropertyValue(p)]));
      Array.from(el.children).forEach((c, i) => walk(c, `${path}>${c.tagName.toLowerCase()}[${i}]`));
    };
    walk(root, root.tagName.toLowerCase());
    return out;
  });
}
