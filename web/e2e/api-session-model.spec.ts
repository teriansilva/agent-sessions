import { expect, type Page, test } from "@playwright/test";
import { clickHeadAction } from "./headActions";
import { mockRoster } from "./roster";

// What an "agent default" API session runs (#1511) — real-browser proof on desktop + mobile.
// The session asked for no model; its client reported gpt-6-codex at "high". The pane's info card
// names that model (not the literal "agent default"), and the session brief shows the same
// "model · level" from every entry point: the pane head, the sidebar row's ⋯ menu and the Overview
// map chip's ⋯ menu. Red on origin/main: the card read "agent default" and the brief had no model.

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const KEY = `codex-api:${ID}`;
const TITLE = "Tighten the diff tokens";
const now = Math.floor(Date.now() / 1000);

const ROW = {
  id: KEY,
  engine: "codex-api",
  uuid: ID,
  short_uuid: ID.slice(0, 8),
  cwd: "/home/u/proj",
  project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
  last_mtime: now - 60,
  first_user_message: "",
  title: TITLE,
  sticky: false,
  archived: false,
  ai_summary: "Measured the light-mode diff contrast.",
  ai_recap: "Measured the contrast.\nRaised the add/del mix.",
  reviewed_at: now - 30,
  review_excluded: false,
  has_draft: false,
  model_requested: null,
  model_effective: null,
};
// What only the one-session lookup carries (#1511): the client's report.
const LOOKUP = { ...ROW, model_configured: "gpt-6-codex", effort_requested: null, effort_configured: "high" };

const SNAPSHOT = {
  session_key: KEY,
  revision: 1,
  event_cursor: 1,
  cwd: "/home/u/proj",
  state: "idle",
  active_turn: null,
  model_requested: null,
  model_effective: null,
  model_configured: "gpt-6-codex",
  effort_requested: null,
  effort_configured: "high",
  turns: [],
  omitted_turns: 0,
  pending_requests: [],
  native: { native_id: "n1", worker: "w1", background_active: false },
  read_only: null,
};

async function mockApp(page: Page) {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/projects**", (r) =>
    r.fulfill({ json: { projects: [{ id: "p1", name: "proj", color: "#ffb000", archived: false }] } }),
  );
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["codex-api"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: ["project:p1"],
        projects_hidden: [],
        project_names: {},
        agent_defaults: { bypass: false },
      },
    }),
  );
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: { sessions: [ROW], next_offset: null, total: 1, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route(new RegExp(`/api/sessions/codex-api(?::|%3A)${ID}$`), (r) =>
    r.fulfill({ json: LOOKUP }),
  );
  const structured = new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}(/[a-z]+)?(\\?.*)?$`);
  await page.route(structured, (r) => {
    const sub = r.request().url().match(structured)?.[1] ?? "";
    if (sub === "/events")
      return r.fulfill({ json: { session_key: KEY, revision: 1, next_cursor: 1, events: [] } });
    if (sub === "") return r.fulfill({ json: SNAPSHOT });
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
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

async function expectBriefModel(page: Page) {
  const dialog = page.getByRole("dialog", { name: TITLE });
  await expect(dialog).toBeVisible();
  const model = dialog.getByTestId("recap-model");
  await expect(model).toHaveText("gpt-6-codex · high");
  // Inside the dialog on every viewport: the line wraps, it is never clipped.
  const [box, frame] = await Promise.all([model.boundingBox(), dialog.boundingBox()]);
  expect(box && frame).toBeTruthy();
  expect(box!.x).toBeGreaterThanOrEqual(frame!.x);
  expect(box!.x + box!.width).toBeLessThanOrEqual(frame!.x + frame!.width + 0.5);
}

test("the pane names the model an agent-default session runs, and its brief says the same", async ({
  page,
}) => {
  await mockApp(page);
  await page.goto(`/s/codex-api/${ID}`);
  await expect(page.getByTestId("structured-model")).toHaveText("gpt-6-codex · high · agent default");
  await clickHeadAction(page, /open session brief/i);
  await expectBriefModel(page);
});

test("the sidebar row's brief shows the same model and level", async ({ page }, testInfo) => {
  await mockApp(page);
  await page.goto("/");
  if (testInfo.project.name === "mobile") await page.locator("header .navToggle").click();
  const row = page.getByRole("listitem").filter({ hasText: TITLE });
  await expect(row).toBeVisible();
  await row.hover();
  await row.getByRole("button", { name: "Session actions" }).click();
  await page
    .getByRole("menu", { name: "Session actions" })
    .getByRole("menuitem", { name: "Open session brief" })
    .click();
  await expectBriefModel(page);
});

test("the Overview map chip's brief shows the same model and level", async ({ page }) => {
  await mockApp(page);
  await page.goto("/overview");
  await page.getByRole("button", { name: /expand all/i }).click();
  await page.getByRole("button", { name: `Session actions: ${TITLE}` }).click();
  await page.getByRole("menuitem", { name: "Open session brief" }).click();
  await expectBriefModel(page);
});
