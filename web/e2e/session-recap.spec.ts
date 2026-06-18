import { expect, test } from "@playwright/test";
import { setupBench } from "./terminal/harness";

// Real-browser coverage for the session-brief modal (#481): the recap icon in the terminal
// header opens a modal showing the full title, summary, and the chronological recap. Runs on
// both the desktop and mobile Playwright projects. Network + WebSocket are fully mocked via the
// terminal bench. Red→green gate: the recap icon/modal does NOT exist on origin/main, so the
// "open session brief" trigger assertion fails before the feature and passes after.

const ENGINE = "claude";
const UUID = "aaaaaaaa-1111-2222-3333-444444444444";
const TITLE = "Fix the auth token refresh-rotation race in the login flow";
const RECAP = [
  "Root-caused intermittent 401s to a token-refresh race.",
  "Added a single-flight lock + regression test (red then green).",
  "Opened PR #482 — now waiting on review.",
].join("\n");

const ROW = {
  id: `${ENGINE}:${UUID}`,
  engine: ENGINE,
  uuid: UUID,
  short_uuid: "aaaaaaaa",
  cwd: "/home/u/proj",
  project: { kind: "folder", id: "/home/u/proj", name: "proj" },
  last_mtime: 1_700_000_000,
  first_user_message: "",
  title: TITLE,
  sticky: false,
  sort_key: 0,
  archived: false,
  ai_summary: "Refactoring the token-refresh path to remove a double-refresh race.",
  ai_title: TITLE,
  intervention_required: true,
  intervention_reason: "waiting on permission to edit prod config",
  reviewed_at: 1_700_000_000,
  review_excluded: false,
  has_draft: false,
  ai_recap: RECAP,
};

test.beforeEach(async ({ page }) => {
  await setupBench(page, { sessions: [{ engine: ENGINE, uuid: UUID, title: TITLE }] });
  // Override ONLY the list endpoint (not /history or /draft) with a row carrying the recap.
  // Registered after the bench route → it wins for the list call; the narrow regex leaves the
  // bench's /history + /draft handling intact.
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        sessions: [ROW],
        next_offset: null,
        total: 1,
        facets: { projects: [ROW.project], engines: [ENGINE] },
      },
    }),
  );
});

test("recap icon opens the session-brief modal with the chronological recap (#481)", async ({
  page,
}) => {
  await page.goto(`/s/${ENGINE}/${UUID}`);

  // The header recap icon (absent on origin/main — the red→green gate).
  const trigger = page.getByRole("button", { name: /open session brief/i });
  await expect(trigger).toBeVisible();
  await trigger.click();

  const dialog = page.getByRole("dialog", { name: /fix the auth token/i });
  await expect(dialog).toBeVisible();
  // Full (untruncated) title + summary + the chronological recap timeline + intervention chip.
  await expect(dialog).toContainText(TITLE);
  await expect(dialog).toContainText("Refactoring the token-refresh path");
  await expect(dialog).toContainText("Root-caused intermittent 401s");
  await expect(dialog).toContainText("Opened PR #482");
  await expect(dialog).toContainText(/needs you/i);

  // Esc closes and returns focus to the trigger.
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
});

test("clicking the backdrop closes the session-brief modal (#481)", async ({ page }) => {
  await page.goto(`/s/${ENGINE}/${UUID}`);
  await page.getByRole("button", { name: /open session brief/i }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  // The backdrop covers the viewport; a corner click lands outside the centered/bottom dialog.
  await page.mouse.click(5, 5);
  await expect(dialog).toBeHidden();
});
