import { expect, test } from "@playwright/test";
import { clickHeadAction } from "./headActions";
import { setupBench } from "./terminal/harness";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

// Real-browser coverage for handing off INTO an API session (#2141). The API tile is a real
// target, preselected API first; the Permissions row is the only new control (enabled for a client
// that can skip prompts, disabled with its reason otherwise); and committing opens the API
// session, whose pane shows the brief as its first turn. The server sends that turn and archives
// the source, so the dialog never calls the archive route for an API target.
// Red→green: on origin/main every API tile is disabled ("no seed-capable start yet"), so the
// first assertion fails before the change. Runs on the desktop and mobile projects.

const SOURCE_ENGINE = "claude";
const SOURCE_UUID = "aaaaaaaa-1111-2222-3333-444444444444";
const TITLE = "Fix the auth token refresh-rotation race";
const BRIEF =
  "# Handoff — continued from a claude session\n\n## Recent turns\n\n[user] run the auth tests again\n[agent] 4 passed — pushed the fix";
const TARGET = "codex-api";
const NATIVE = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const TURN = "6a1f2c4e-0000-4b7e-9d3a-0f5c8e7b2141";
const SESSION = new RegExp(
  `/api/structured/sessions/${TARGET}(?::|%3A)${NATIVE}(/[a-z]+)?(\\?.*)?$`,
);

const READY_API = (kind: string, source: string, canBypass: boolean) => ({
  present: true,
  supports_new: true,
  supports_seed_start: true,
  seed_reason: null,
  api: { kind, source, unavailable_reason: null, can_bypass: canBypass },
});

function snapshot() {
  return {
    session_key: `${TARGET}:${NATIVE}`,
    revision: 3,
    event_cursor: 3,
    cwd: "/home/u/proj",
    state: "idle",
    active_turn: null,
    model_requested: null,
    model_effective: "gpt-5-codex",
    turns: [
      {
        kind: "operator",
        turn_id: TURN,
        operation_id: TURN,
        state: "completed",
        text: BRIEF,
        ts: 1_790_000_000,
        reply: "Picking up the auth fix from here.",
        reply_ts: 1_790_000_010,
        text_truncated: false,
        reply_truncated: false,
        reason: null,
        tools: [],
        tools_truncated: false,
        attachments: [],
        context: {},
      },
    ],
    omitted_turns: 0,
    history_before: null,
    pending_requests: [],
    native: { native_id: "n1", worker: "w1", background_active: false },
    read_only: null,
  };
}

test.beforeEach(async ({ page }) => {
  await setupBench(page, {
    sessions: [{ engine: SOURCE_ENGINE, uuid: SOURCE_UUID, title: TITLE }],
  });
  await page.route("**/api/handoff/prepare", (r) =>
    r.fulfill({
      json: {
        handle: "h-api-1",
        preview: BRIEF,
        meta: { mode: "quick", turns: 2, bytes: BRIEF.length, cap: 8192 },
      },
    }),
  );
  await page.route(SESSION, (r) => {
    const sub = new URL(r.request().url()).pathname.match(/\/(events|history)$/)?.[1] ?? "";
    if (sub === "events") {
      return r.fulfill({
        json: { session_key: `${TARGET}:${NATIVE}`, revision: 3, next_cursor: 3, events: [] },
      });
    }
    if (sub === "") return r.fulfill({ json: snapshot() });
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
  await mockRoster(page, {
    only: ["claude", "codex", "codex-api", "opencode2-api", "gemini", "shell"],
    overrides: {
      "codex-api": READY_API("codex-app-server", "codex", true),
      "opencode2-api": READY_API("opencode2-acp", "opencode2", false),
    },
    families: [
      {
        family: "codex",
        clients: ["codex", "codex-api"],
        interactive_default: { engine: "codex-api", reason: "api_ready" },
        unattended_admitted: [],
        reasons: {},
      },
    ],
  });
});

test("an API client is a handoff target: preselected, committed with its options, opened with the brief (#2141)", async ({
  page,
}) => {
  let committed: Record<string, unknown> | null = null;
  const archives: string[] = [];
  await page.route(/\/api\/handoff$/, async (route) => {
    committed = route.request().postDataJSON();
    await route.fulfill({
      json: {
        id: `${TARGET}:${NATIVE}`,
        engine: TARGET,
        native: NATIVE,
        cwd: "/home/u/proj",
        api: true,
        bypass: false,
        delivery: "sent",
      },
    });
  });
  await page.route(/\/api\/sessions\/[^/]+\/archive$/, async (route) => {
    archives.push(route.request().url());
    await route.fulfill({ json: { archived: true } });
  });

  // Only codex-api is a ready API client here (roster order would otherwise put opencode2-api
  // first): it is preselected as the first ready API client. Registered last, so it wins.
  await mockRoster(page, {
    only: ["claude", "codex", "codex-api", "opencode2-api", "gemini", "shell"],
    overrides: {
      "codex-api": READY_API("codex-app-server", "codex", true),
      "opencode2-api": {
        supports_seed_start: false,
        seed_reason: "OpenCode 2 is not installed",
      },
    },
  });
  await page.goto(`/s/${SOURCE_ENGINE}/${SOURCE_UUID}`);
  await clickHeadAction(page, /hand off session/i);
  const dialog = page.getByRole("dialog", { name: /hand off/i });
  await expect(dialog).toBeVisible();

  // The API client is enabled and preselected (API first); the console client stays one tap away;
  // an API client that cannot start stays disabled with the server's reason.
  await expect(dialog.getByRole("radio", { name: /opencode2-api/i })).toBeDisabled();
  await expect(dialog.getByRole("radio", { name: /opencode2-api/i })).toContainText(
    /not installed/i,
  );
  const api = dialog.getByRole("radio", { name: /^codex-api$/i });
  await expect(api).toBeEnabled();
  await expect(api).toHaveAttribute("aria-checked", "true");
  await expect(dialog.getByRole("radio", { name: /^codex$/i })).toBeEnabled();

  // The Permissions row: a labelled, keyboard-reachable checkbox, unticked by default (fail closed).
  const skip = dialog.getByRole("checkbox", { name: /skip permission prompts/i });
  await expect(skip).toBeEnabled();
  await expect(skip).not.toBeChecked();
  const row = skip.locator("xpath=ancestor::label[1]");
  const box = await row.boundingBox();
  expect(box?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_FLOOR); // the design's touch target
  await skip.focus();
  await expect(skip).toBeFocused();

  await dialog.getByRole("button", { name: /^hand off$/i }).click();
  await page.waitForURL(`**/s/${TARGET}/${NATIVE}`);
  expect(committed).toEqual({ handle: "h-api-1", bypass: false, archive_source: true });
  // The brief is the new session's first turn, shown by its pane.
  await expect(page.getByText("run the auth tests again").first()).toBeVisible();
  // The server archives the source once the brief is accepted; the dialog never does.
  expect(archives).toEqual([]);
});

test("a client that can't skip prompts shows the Permissions row disabled with its reason (#2141)", async ({
  page,
}) => {
  await page.goto(`/s/${SOURCE_ENGINE}/${SOURCE_UUID}`);
  await clickHeadAction(page, /hand off session/i);
  const dialog = page.getByRole("dialog", { name: /hand off/i });
  await dialog.getByRole("radio", { name: /^opencode2-api$/i }).click();
  const skip = dialog.getByRole("checkbox", { name: /skip permission prompts/i });
  await expect(skip).toBeDisabled();
  await expect(skip).not.toBeChecked();
  await expect(dialog).toContainText(/can't skip prompts yet/i);
});
