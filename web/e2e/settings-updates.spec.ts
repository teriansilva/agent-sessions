import { expect, test, type Page } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";
import { TOUCH_FLOOR } from "./touchTarget";

// #538 / #1994: real-browser proof for the in-app automatic-update controls in Settings →
// Updates. Automatic updates are ON by default (BattleLab and its installed agents): one status
// line, Pause behind a confirmation, Resume. They and the release-channel radiogroup persist via
// POST /api/update/settings (mocked here — the server semantics are covered by
// tests/test_api.py + tests/test_update*.py + tests/test_agent_autoupdate.py). The mobile
// project additionally proves the card reflows without horizontal scroll on a phone viewport.

async function setup(page: Page): Promise<unknown[]> {
  const posts: unknown[] = [];
  // An install that never chose: automatic updates are on (#1994).
  const state = { auto_update: true, channel: "stable" };
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
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
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "1.2.3" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) =>
    r.fulfill({ json: { os: "Linux" } }),
  );
  await page.route("**/api/update/settings", (r) => {
    if (r.request().method() === "POST") {
      const body = r.request().postDataJSON() as {
        auto_update?: boolean;
        channel?: string;
      };
      posts.push(body);
      state.auto_update = body.auto_update ?? state.auto_update;
      state.channel = body.channel ?? state.channel;
      return r.fulfill({ json: { ...state, last_auto: null } });
    }
    return r.fulfill({ json: { ...state, last_auto: null } });
  });
  await page.goto(settingsPath("updates"));
  return posts;
}

test.describe("automatic updates: on by default, Pause asks first, Resume (#538, #1994)", () => {
  test("the status line, Pause → confirm → Paused → Resume, and the channel persist", async ({
    page,
  }) => {
    const posts = await setup(page);
    const status = page.getByTestId("auto-update-status");
    await expect(status).toHaveText("Automatic updates: on");
    // No opt-in switch any more: nobody goes into Settings to turn updates on.
    await expect(page.getByRole("checkbox", { name: /automatic updates/i })).toHaveCount(0);
    await expect(
      page.getByText(/BattleLab and your installed agents update themselves daily/i),
    ).toBeVisible();

    // Pause asks first; Cancel changes nothing.
    const pause = page.getByRole("button", { name: "Pause", exact: true });
    await pause.click();
    const dialog = page.getByRole("dialog", { name: "Pause automatic updates?" });
    await expect(dialog).toContainText("The catalog is still checked");
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toHaveCount(0);
    await expect(status).toHaveText("Automatic updates: on");
    expect(posts).toEqual([]);

    await pause.click();
    await page.getByRole("dialog").getByRole("button", { name: "Pause updates" }).click();
    await expect(status).toHaveText("Automatic updates: paused");
    await expect(page.getByText(/nothing installs until you resume/i)).toBeVisible();
    await page.getByRole("button", { name: "Resume" }).click();
    await expect(status).toHaveText("Automatic updates: on");

    const main = page.getByRole("radio", { name: /main/i });
    const stable = page.getByRole("radio", { name: /stable/i });
    await expect(stable).toHaveAttribute("aria-checked", "true");
    await main.click();
    await expect(main).toHaveAttribute("aria-checked", "true");
    await expect(stable).toHaveAttribute("aria-checked", "false");

    expect(posts).toEqual([{ auto_update: false }, { auto_update: true }, { channel: "main" }]);
  });

  test("Pause, its confirmation and Resume work from the keyboard alone, at touch size", async ({
    page,
  }) => {
    const posts = await setup(page);
    const pause = page.getByRole("button", { name: "Pause", exact: true });
    await expect(pause).toBeVisible();
    expect((await pause.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    await pause.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: "Pause automatic updates?" });
    await expect(dialog).toBeVisible();
    // Focus lands on the safe action.
    await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();
    for (const button of await dialog.getByRole("button", { name: /^(Cancel|Pause updates)$/ }).all()) {
      expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
    // Escape cancels.
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(pause).toBeFocused();
    await page.keyboard.press("Enter");
    const confirm = page.getByRole("dialog").getByRole("button", { name: "Pause updates" });
    await confirm.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("auto-update-status")).toHaveText("Automatic updates: paused");
    const resume = page.getByRole("button", { name: "Resume" });
    // Focus is not lost with the Pause button gone: it lands on Resume.
    await expect(resume).toBeFocused();
    expect((await resume.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("auto-update-status")).toHaveText("Automatic updates: on");
    expect(posts).toEqual([{ auto_update: false }, { auto_update: true }]);
  });

  test("the Updates page has no horizontal scroll with the grown Updates card", async ({
    page,
  }, testInfo) => {
    test.skip(
      testInfo.project.name !== "mobile",
      "the single-column reflow is a mobile concern",
    );
    await setup(page);
    await expect(page.getByTestId("auto-update-status")).toBeVisible();
    await page.getByRole("button", { name: "Pause", exact: true }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    const overflow = await page.evaluate(
      () =>
        document.documentElement.scrollWidth -
        document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
});

// #1085: an update shows its progress — step, bar, elapsed — THROUGH the restart it causes, and
// What's new links the release notes on stable. The server is mocked: the progress sequence is
// what `GET /api/update/progress` returns while the installer runs, and a dropped request stands
// in for the restart window (the real server is down for a moment there).
test("Update now shows the installer's progress through the restart, and links the release notes (#1085)", async ({
  page,
}) => {
  const now = () => Math.floor(Date.now() / 1000);
  const started = now();
  let applied = false;
  let reads = 0;
  const run = (step: string, index: number, label: string) => ({
    state: "running",
    steps: 7,
    step,
    step_index: index,
    label,
    started_at: started,
    elapsed_s: now() - started,
    last_duration_s: 360,
  });
  await page.route("**/api/update/check", (r) =>
    r.fulfill({
      json: { current: "1.2.3", channel: "stable", latest: "v1.3.0", update_available: true },
    }),
  );
  await page.route("**/api/update/apply", (r) => {
    applied = true;
    return r.fulfill({ status: 202, json: { status: "updating" } });
  });
  await page.route("**/api/update/progress", (r) => {
    if (!applied) return r.fulfill({ json: { state: "idle", steps: 7, last_duration_s: 360 } });
    reads += 1;
    if (reads === 1) return r.fulfill({ json: run("web", 4, "Building the web UI") });
    if (reads === 2) return r.fulfill({ json: run("restart", 6, "Restarting the service") });
    if (reads === 3) return r.abort("connectionrefused");
    return r.fulfill({
      json: {
        ...run("health", 7, "Checking it came back"),
        state: "done",
        elapsed_s: now() - started + 1,
      },
    });
  });
  await setup(page);

  // Before anything runs: the last duration, and What's new with the installed release's notes.
  await expect(page.getByTestId("update-last-duration")).toHaveText("The last update took 6 min.");
  const whatsNew = page.getByTestId("update-whats-new");
  await expect(whatsNew).toBeVisible();
  await expect(page.getByTestId("update-release-notes")).toHaveAttribute(
    "href",
    "https://github.com/teriansilva/agent-sessions/releases/tag/v1.2.3",
  );

  await page.getByRole("button", { name: /check for updates/i }).click();
  await expect(page.getByText("Update available: v1.3.0")).toBeVisible();
  // …and now the notes link is the release on offer.
  await expect(page.getByTestId("update-release-notes")).toHaveAttribute(
    "href",
    "https://github.com/teriansilva/agent-sessions/releases/tag/v1.3.0",
  );

  await page.getByTestId("update-apply").click();
  const panel = page.getByTestId("update-progress");
  await expect(panel).toContainText("Step 4 of 7 · Building the web UI");
  await expect(panel).toContainText("last update took 6 min");
  await expect(page.getByRole("progressbar", { name: "Update progress" })).toHaveAttribute(
    "aria-valuenow",
    "4",
  );
  await expect(panel).toContainText("Step 6 of 7 · Restarting the service");
  // The dropped read is the restart, not an error.
  await expect(panel).toContainText("Restarting… reconnecting");
  await expect(panel).toContainText("Update finished");
  await expect(page.getByTestId("update-reload")).toBeVisible();
  // Terminal: the panel stops asking.
  const settled = reads;
  await page.waitForTimeout(4_500);
  expect(reads).toBe(settled);
  // No sideways scroll on a phone.
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    page.viewportSize()!.width,
  );
});

// #832: an update REFUSED by release verification says so, with the reason — it used to reach the
// card as a 503 and read "Self-update isn't available for this install", which is the wrong
// fault entirely. The long refusal must also wrap on a phone rather than widen the page.
test("a refused update shows why, not 'unavailable' (#832)", async ({ page }) => {
  const reason =
    "refusing to update: v1.3.0: validly signed, but by a key this installation does not trust";
  let refused = false;
  await page.route("**/api/update/check", (r) =>
    r.fulfill({
      json: {
        current: "1.2.3",
        channel: "stable",
        latest: "v1.3.0",
        update_available: true,
        ...(refused ? { blocked: reason } : {}),
      },
    }),
  );
  await page.route("**/api/update/apply", (r) => {
    refused = true;
    return r.fulfill({ status: 422, json: { detail: reason } });
  });
  await page.route("**/api/update/progress", (r) =>
    r.fulfill({ json: { state: "idle", steps: 7, last_duration_s: 360 } }),
  );
  await setup(page);
  await page.getByRole("button", { name: /check for updates/i }).click();
  await page.getByTestId("update-apply").click();

  const blocked = page.getByTestId("update-blocked");
  await expect(blocked).toBeVisible();
  await expect(blocked).toContainText("does not trust");
  await expect(page.getByText(/the update was refused/i)).toBeVisible();
  await expect(page.getByText(/isn.t available for this install/i)).toHaveCount(0);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});
