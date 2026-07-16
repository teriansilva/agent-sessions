import { expect, test } from "@playwright/test";
import { setupBench } from "./terminal/harness";

// Real-browser coverage for the cross-engine handoff modal (#597, Phase 1): the "Hand off"
// control in the terminal header opens a modal with a capability-driven engine picker, the
// Quick-mode seed preview, and a confirm that navigates to the freshly minted target session.
// Runs on both the desktop and mobile Playwright projects; network + WebSocket are fully
// mocked via the terminal bench. Red→green gate: the "hand off session" trigger does not
// exist on origin/main, so the trigger assertion fails before the feature and passes after.

const ENGINE = "claude";
const UUID = "aaaaaaaa-1111-2222-3333-444444444444";
const TITLE = "Fix the auth token refresh-rotation race";
const PREVIEW =
  "# Handoff — continued from a claude session\n\n## Recent turns\n\n[user] run the auth tests again\n[agent] 4 passed — pushed the fix";
const TARGET_NATIVE = "new-bbbbbbbb-1111-2222-3333-444444444444";

const ENGINES = [
  { id: "claude", present: true, supports_new: true, supports_seed_start: true, seed_reason: null, bin: "/bin/claude" },
  { id: "codex", present: true, supports_new: true, supports_seed_start: true, seed_reason: null, bin: "/bin/codex" },
  { id: "gemini", present: true, supports_new: true, supports_seed_start: false, seed_reason: "no seed-capable start yet", bin: "/bin/gemini" },
  { id: "shell", present: true, supports_new: true, supports_seed_start: false, seed_reason: "not an agent engine", bin: "/bin/bash" },
];

test.beforeEach(async ({ page }) => {
  await setupBench(page, { sessions: [{ engine: ENGINE, uuid: UUID, title: TITLE }] });
  // Registered after the bench routes → these win for their endpoints.
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: ENGINES } }));
  await page.route("**/api/handoff/prepare", (r) =>
    r.fulfill({
      json: {
        handle: "h-e2e-1",
        preview: PREVIEW,
        meta: { mode: "quick", turns: 2, bytes: PREVIEW.length, cap: 8192 },
      },
    }),
  );
  await page.route(/\/api\/handoff$/, (r) =>
    r.fulfill({
      json: {
        id: `codex:${TARGET_NATIVE}`,
        engine: "codex",
        native: TARGET_NATIVE,
        cwd: "/home/u/proj",
      },
    }),
  );
});

test("hand-off control opens the modal; picker + preview render; confirm lands on the new session (#597)", async ({
  page,
}) => {
  await page.goto(`/s/${ENGINE}/${UUID}`);

  // The header control (absent on origin/main — the red→green gate).
  const trigger = page.getByRole("button", { name: /hand off session/i });
  await expect(trigger).toBeVisible();
  await trigger.click();

  const dialog = page.getByRole("dialog", { name: /hand off/i });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(TITLE); // FROM // line names the source

  // Capability-driven tiles: codex preselected (first non-source enabled engine); gemini
  // disabled with the server-supplied reason; the non-agent shell is never offered.
  const codex = dialog.getByRole("radio", { name: /codex/i });
  await expect(codex).toHaveAttribute("aria-checked", "true");
  const gemini = dialog.getByRole("radio", { name: /gemini/i });
  await expect(gemini).toBeDisabled();
  await expect(gemini).toContainText(/no seed-capable start yet/i);
  await expect(dialog.getByRole("radio", { name: /shell/i })).toHaveCount(0);

  // Quick-mode seed preview is shown read-only; AI summary is visibly Phase 2.
  const preview = dialog.getByLabel(/seed preview/i);
  await expect(preview).toHaveValue(PREVIEW);
  await expect(preview).toHaveAttribute("readonly", "");
  await expect(dialog.getByRole("button", { name: /ai summary/i })).toBeDisabled();
  await expect(dialog).toContainText(/never argv or url/i);

  // Confirm → navigate to the freshly minted target session (normal fresh-launch route).
  await dialog.getByRole("button", { name: /^hand off$/i }).click();
  await page.waitForURL(`**/s/codex/${TARGET_NATIVE}`);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("Escape closes the hand-off modal and returns focus to the trigger (#597)", async ({
  page,
}) => {
  await page.goto(`/s/${ENGINE}/${UUID}`);
  const trigger = page.getByRole("button", { name: /hand off session/i });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: /hand off/i });
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
});

test("backdrop click closes the hand-off modal (#597)", async ({ page }) => {
  await page.goto(`/s/${ENGINE}/${UUID}`);
  await page.getByRole("button", { name: /hand off session/i }).click();
  const dialog = page.getByRole("dialog", { name: /hand off/i });
  await expect(dialog).toBeVisible();
  await page.mouse.click(5, 5);
  await expect(dialog).toBeHidden();
});
